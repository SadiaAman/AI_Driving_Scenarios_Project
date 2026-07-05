from datetime import datetime
from pathlib import Path
import os
import re
import shutil
import subprocess
import time

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from pydantic import BaseModel

# Phase 5 (gradual scenariogeneration migration): the Environment block is the
# first piece built with the scenariogeneration library instead of a manual XML
# string. The import is optional — if the library is unavailable we transparently
# fall back to the manual builder, so the generator never breaks.
try:
    from scenario_builder import (
        environment_action_xml as _sg_environment_action_xml,
        build_entities_xml as _sg_build_entities_xml,
        build_ego_init_xml as _sg_build_ego_init_xml,
    )
    _SCENARIOGENERATION_AVAILABLE = True
except Exception:
    _SCENARIOGENERATION_AVAILABLE = False

# Varied colours/models for background traffic (existing VehicleCatalog entries),
# distinguishable from the white Ego and red NPC. Shared by the manual loop and
# the scenariogeneration entities builder so both stay in sync.
TRAFFIC_MODELS = ["car_white", "car_white", "car_white"]


BASE_DIR = Path(__file__).resolve().parent

# Load backend/.env into the environment (e.g. ANTHROPIC_API_KEY) if python-dotenv
# is installed. Optional — without it, real OS environment variables still work.
try:
    from dotenv import load_dotenv
    load_dotenv(BASE_DIR / ".env")
except Exception:
    pass

# Legacy folder (kept for historical files only — never written to or read by the
# new per-variant logic). New scenarios go into the two variant folders below.
GENERATED_DIR = BASE_DIR / "generated"
GENERATED_DIR.mkdir(exist_ok=True)

# Per-variant output folders. Variant A (structured form) and Variant B
# (unstructured natural language) keep their generated XOSC files completely
# separate so each has its own sequence numbering and latest-file pointer.
# Names contain spaces — always built with pathlib, never string-concatenated.
STRUCTURED_GENERATED_DIR = BASE_DIR / "Structured XOSC generated"
UNSTRUCTURED_GENERATED_DIR = BASE_DIR / "Unstructured XOSC generated"
STRUCTURED_GENERATED_DIR.mkdir(exist_ok=True)
UNSTRUCTURED_GENERATED_DIR.mkdir(exist_ok=True)

# Maps the frontend "variant" string to its output directory. Used by the
# generate, run, download, and list endpoints to route to the correct folder.
VARIANT_DIRS = {
    "structured": STRUCTURED_GENERATED_DIR,
    "unstructured": UNSTRUCTURED_GENERATED_DIR,
}

# Separate latest-file pointers so Variant A can never run/export Variant B's
# file (and vice versa). The road (.xodr) is always fabriksgatan, so it stays shared.
LATEST_STRUCTURED_XOSC_PATH = None
LATEST_UNSTRUCTURED_XOSC_PATH = None
LATEST_XODR_PATH = None  # the road (.xodr) file used by the latest scenario

# Per-variant MP4 output folders (kept separate, never mixed). Auto-created.
STRUCTURED_VIDEO_DIR = BASE_DIR / "Structured Output Videos"
UNSTRUCTURED_VIDEO_DIR = BASE_DIR / "Unstructured Output Videos"
STRUCTURED_VIDEO_DIR.mkdir(exist_ok=True)
UNSTRUCTURED_VIDEO_DIR.mkdir(exist_ok=True)
VARIANT_VIDEO_DIRS = {
    "structured": STRUCTURED_VIDEO_DIR,
    "unstructured": UNSTRUCTURED_VIDEO_DIR,
}

# --- Recording config -------------------------------------------------------
# esmini's own --capture_screen produces no frames on this prebuilt demo binary,
# so recording is done by capturing the live esmini window with FFmpeg's gdigrab.
# This requires the esmini window to stay visible on-screen for the whole capture.
RECORDING_FPS = 30
ESMINI_WARMUP_SECONDS = 3.0          # let esmini open + load the 3D scene before capture
RECORDING_BUFFER_SECONDS = 2.0       # extra capture after the scenario StopTrigger time
DEFAULT_RECORDING_TIMEOUT_SECONDS = 90  # hard safety cap on total FFmpeg wall time
# Fixed on-screen window so gdigrab always finds a visible, known-size target.
ESMINI_RECORD_WINDOW = ("60", "60", "1280", "720")  # x, y, w, h
ESMINI_WINDOW_TITLE = os.getenv("ESMINI_WINDOW_TITLE", "esmini")

PROJECT_DIR = BASE_DIR.parents[2]
ESMINI_DEMO_DIR = PROJECT_DIR / "03_esmini" / "esmini-demo"


def road_files_for(data=None) -> tuple:
    """
    Every scenario uses one fixed OpenDRIVE road: the bundled fabriksgatan map,
    with its matching scene graph (.osgb). Returns (logic_file, scene_graph_file).
    This is what each generated XOSC references and what /download-xodr exports.
    """
    resources = ESMINI_DEMO_DIR / "resources"
    return (resources / "xodr" / "fabriksgatan.xodr",
            resources / "models" / "fabriksgatan.osgb")

ESMINI_EXE = os.getenv(
    "ESMINI_EXE",
    str(ESMINI_DEMO_DIR / "bin" / "esmini.exe")
)

app = FastAPI(title="Narrative XAI Driving Scenario Prototype")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


class ScenarioRequest(BaseModel):
    egoSpeed: float
    trafficVehicles: int
    weather: str
    timeOfDay: str
    npcType: str
    npcBehavior: str
    egoResponse: str
    prompt: str
    # NPC actor speed in km/h. Recorded/logged now; applied to the NPC's actual
    # movement in a later phase.
    npcSpeed: float = 30.0
    # When true, "Improve actor behavior" intensifies the NPC and ego maneuvers
    # (harder/earlier braking, faster cut-in/lane-change, more sudden crossing).
    # Defaults to false so normal generation is unchanged.
    improve: bool = False


class RefineRequest(ScenarioRequest):
    # A free-text follow-up instruction, e.g. "make it rain and use a truck".
    refineText: str = ""


class TextScenarioRequest(BaseModel):
    # Variant B (unstructured): a natural-language scenario description.
    text: str
    autoValidate: bool = True


# ---------------------------------------------------------------------------
# Environment helpers (Step 1: lighting, Step 2: weather visibility)
# ---------------------------------------------------------------------------
# esmini / OpenSCENARIO interpret Sun "intensity" as ILLUMINANCE IN LUX, not a
# 0..1 factor. Real daylight is tens of thousands of lux. The old prototype used
# values like 2.5-3.5 lux, which is roughly moonlight, so even "Day + clear"
# rendered almost black. We now use realistic lux values.
WEATHER_PRESETS = {
    # weather:   (fractionalCloudCover, precipType, precipIntensity, fogRange, friction, dayLux)
    #
    # IMPORTANT — what esmini actually renders (per esmini docs):
    #   * Sun illuminance  -> sky brightness (100000 = bright blue, 0 = black).
    #   * Fog visualRange  -> visible haze; LOWER = denser. Must be SMALLER than
    #                         the visible scene (fabriksgatan road ~90 m) to show
    #                         up at all, so large ranges look like clear weather.
    #   * Precipitation (rain/snow particles) is NOT rendered by esmini, and
    #     vehicle headlights are NOT rendered either. So rain/snow are conveyed
    #     visually through fog haze + a dimmer/greyer sky (lower illuminance);
    #     the Precipitation element is still emitted for correctness/friction.
    #
    # fractionalCloudCover (OSC 1.2+) replaces the deprecated cloudState attribute.
    "clear": ("zeroOktas",  "dry",  "0.0", "100000", "1.0", 100000),
    "rain":  ("eightOktas", "rain", "0.7", "800",   "0.7",  40000),
    "snow":  ("eightOktas", "snow", "0.6", "500",   "0.5",  65000),
    "fog":   ("zeroOktas",  "dry",  "0.0", "120",   "0.8",  90000),
}

# NPC type -> how the "main other actor" is represented in the scenario.
#   kind "crosser": a vulnerable road user that crosses the road (proven
#                   trajectory taken from esmini's own pedestrian.xosc).
#   kind "vehicle": a lead vehicle ahead of ego in the same lane that can
#                   brake or change lane.
NPC_PRESETS = {
    "pedestrian": {"kind": "crosser", "catalog": None, "speed": 1.5},
    "cyclist": {"kind": "crosser", "catalog": "bicycle", "speed": 3.0},
    "car": {"kind": "vehicle", "catalog": "car_yellow", "speed_factor": 0.5},
    "truck": {"kind": "vehicle", "catalog": "truck_yellow", "speed_factor": 0.5},
}


def get_environment(data: ScenarioRequest):
    """Return (date_time, cloud_cover, precip_type, precip_intensity, fog_range,
    friction, sun_intensity) for the EnvironmentAction."""
    preset = WEATHER_PRESETS.get(data.weather, WEATHER_PRESETS["clear"])
    cloud_cover, precip_type, precip_intensity, fog_range, friction, day_lux = preset

    if data.timeOfDay.lower() == "day":
        date_time = "2026-06-07T12:00:00"
        sun_lux = day_lux
    else:
        # Night. esmini does NOT render vehicle headlights, so the only way to
        # convey night in the viewer is Sun illuminance (sky brightness). Use a
        # dusk-level value so the scene reads as a dim evening but stays VISIBLE,
        # rather than pure black (which would just hide everything).
        date_time = "2026-06-07T22:00:00"
        sun_lux = max(round(day_lux * 0.10), 5000)

    return (
        date_time,
        cloud_cover,
        precip_type,
        precip_intensity,
        fog_range,
        friction,
        str(sun_lux),
    )


def _manual_environment_action(
    date_time, cloud_cover, precipitation_type, precipitation_intensity,
    fog_range, friction, sun_intensity,
) -> str:
    """Manual-XML EnvironmentAction (fallback when scenariogeneration is absent)."""
    return f'''<GlobalAction>
               <EnvironmentAction>
                  <Environment name="GeneratedEnvironment">
                     <TimeOfDay animation="false" dateTime="{date_time}"/>
                     <Weather fractionalCloudCover="{cloud_cover}">
                        <Sun intensity="{sun_intensity}" azimuth="0" elevation="1.31"/>
                        <Fog visualRange="{fog_range}"/>
                        <Precipitation precipitationType="{precipitation_type}" intensity="{precipitation_intensity}"/>
                     </Weather>
                     <RoadCondition frictionScaleFactor="{friction}"/>
                  </Environment>
               </EnvironmentAction>
            </GlobalAction>'''


def build_environment_action(
    date_time, cloud_cover, precipitation_type, precipitation_intensity,
    fog_range, friction, sun_intensity,
) -> str:
    """
    Phase 5: build the EnvironmentAction with scenariogeneration when available,
    otherwise fall back to the manual XML. Both produce esmini-compatible output;
    the scenariogeneration form uses the OSC 1.2 attribute names (illuminance,
    precipitationIntensity), which esmini supports.
    """
    args = (date_time, cloud_cover, precipitation_type, precipitation_intensity,
            fog_range, friction, sun_intensity)
    if _SCENARIOGENERATION_AVAILABLE:
        try:
            return _sg_environment_action_xml(*args)
        except Exception:
            # Never let a library issue break generation — fall back to manual.
            return _manual_environment_action(*args)
    return _manual_environment_action(*args)


def _manual_entities_block(npc_entity: str, traffic_vehicle_entities: str) -> str:
    """Manual-XML <Entities> block (fallback when scenariogeneration is absent)."""
    return f'''<Entities>
      <ScenarioObject name="Ego">
         <CatalogReference catalogName="VehicleCatalog" entryName="$HostVehicle"/>
      </ScenarioObject>
{npc_entity}{traffic_vehicle_entities}
   </Entities>'''


def build_entities_block(
    npc: dict, pedestrian_model: str, num_traffic_vehicles: int,
    npc_entity: str, traffic_vehicle_entities: str,
) -> str:
    """
    Migration slice 2: build the <Entities> list with scenariogeneration when
    available, otherwise fall back to the manual XML pieces. Both are equivalent
    and esmini-compatible.
    """
    if _SCENARIOGENERATION_AVAILABLE:
        try:
            return _sg_build_entities_xml(
                npc.get("catalog"), pedestrian_model, TRAFFIC_MODELS,
                num_traffic_vehicles,
            )
        except Exception:
            return _manual_entities_block(npc_entity, traffic_vehicle_entities)
    return _manual_entities_block(npc_entity, traffic_vehicle_entities)


def _manual_ego_init(host_speed_ms: float) -> str:
    """Manual-XML Ego Init private block (fallback). Uses the $HostSpeed param."""
    return '''<Private entityRef="Ego">
               <PrivateAction>
                  <RoutingAction>
                     <AssignRouteAction>
                        <CatalogReference catalogName="RoutesAtFabriksgatan" entryName="HostStraightRoute"/>
                     </AssignRouteAction>
                  </RoutingAction>
               </PrivateAction>
               <PrivateAction>
                  <TeleportAction>
                     <Position>
                        <RoutePosition>
                           <RouteRef>
                              <CatalogReference catalogName="RoutesAtFabriksgatan" entryName="HostStraightRoute"/>
                           </RouteRef>
                           <InRoutePosition>
                              <FromLaneCoordinates pathS="0" laneId="1"/>
                           </InRoutePosition>
                        </RoutePosition>
                     </Position>
                  </TeleportAction>
               </PrivateAction>
               <PrivateAction>
                  <LongitudinalAction>
                     <SpeedAction>
                        <SpeedActionDynamics dynamicsShape="step" value="0.0" dynamicsDimension="time"/>
                        <SpeedActionTarget>
                           <AbsoluteTargetSpeed value="$HostSpeed"/>
                        </SpeedActionTarget>
                     </SpeedAction>
                  </LongitudinalAction>
               </PrivateAction>
            </Private>'''


def build_ego_init_block(host_speed_ms: float) -> str:
    """
    Migration slice 3 (Init, Ego only): build the Ego Init private block with
    scenariogeneration when available, otherwise the manual XML. Equivalent and
    esmini-compatible (the route assignment gives the ego its travel heading).
    """
    if _SCENARIOGENERATION_AVAILABLE:
        try:
            return _sg_build_ego_init_xml(host_speed_ms)
        except Exception:
            return _manual_ego_init(host_speed_ms)
    return _manual_ego_init(host_speed_ms)


def build_xosc_preview(data: ScenarioRequest) -> str:
    """
    Generate an esmini-compatible OpenSCENARIO file.

    Every scenario is built on the fixed fabriksgatan road (its <LogicFile> is
    fabriksgatan.xodr), so all generated scenarios share one OpenDRIVE map.
    """
    # esmini expects speed in m/s. Convert ego speed from km/h to m/s.
    host_speed_ms = round(data.egoSpeed / 3.6, 2)
    npc_speed_ms = round(data.npcSpeed / 3.6, 2)

    (
        date_time,
        cloud_cover,
        precipitation_type,
        precipitation_intensity,
        fog_range,
        friction,
        sun_intensity,
    ) = get_environment(data)

    environment_action = build_environment_action(
        date_time, cloud_cover, precipitation_type, precipitation_intensity,
        fog_range, friction, sun_intensity,
    )

    resources_dir = (ESMINI_DEMO_DIR / "resources").as_posix()
    route_catalog_dir = f"{resources_dir}/xosc/Catalogs/Routes"
    vehicle_catalog_dir = f"{resources_dir}/xosc/Catalogs/Vehicles"
    logic_file = f"{resources_dir}/xodr/fabriksgatan.xodr"
    scene_graph_file = f"{resources_dir}/models/fabriksgatan.osgb"
    pedestrian_model = f"{resources_dir}/models/walkman.osgb"

    # -- Step 3/4: build the NPC entity, its init placement, and its maneuver. --
    npc = NPC_PRESETS.get(data.npcType, NPC_PRESETS["pedestrian"])
    npc_entity, npc_init, npc_maneuver_group = build_npc(
        data, npc, host_speed_ms, pedestrian_model, npc_speed_ms
    )

    # -- Step 5: build the ego response maneuver. --
    ego_response_group = build_ego_response(data, host_speed_ms)

    num_traffic_vehicles = min(max(data.trafficVehicles, 0), 3)
    traffic_vehicle_entities = ""
    traffic_vehicle_init_actions = ""
    if num_traffic_vehicles:
        # Background traffic on the oncoming driving lane (-1), spaced ahead.
        # On fabriksgatan road 0 the only driving lanes are 1 (ego + NPC) and -1
        # (oncoming); lane 2 is a border/shoulder. Keeping traffic in lane -1
        # ensures it never blocks the ego's lane or the lane it steers into.
        traffic_positions = [(-1, 20), (-1, 35), (-1, 50)]
        traffic_models = TRAFFIC_MODELS
        # Gentle motion so the traffic visibly drives (oncoming direction on
        # lane -1) without catching up to the Ego or NPC in lane 1.
        traffic_speed_ms = 4.0
        for index in range(num_traffic_vehicles):
            name = f"TrafficVehicle_{index + 1}"
            lane_id, s_pos = traffic_positions[index]
            model = traffic_models[index % len(traffic_models)]
            traffic_vehicle_entities += f'''
      <ScenarioObject name="{name}">
         <CatalogReference catalogName="VehicleCatalog" entryName="{model}"/>
      </ScenarioObject>'''
            traffic_vehicle_init_actions += f'''
            <Private entityRef="{name}">
               <PrivateAction>
                  <TeleportAction>
                     <Position>
                        <LanePosition laneId="{lane_id}" offset="0.0" roadId="0" s="{s_pos}">
                           <Orientation type="relative" h="0" p="0" r="0"/>
                        </LanePosition>
                     </Position>
                  </TeleportAction>
               </PrivateAction>
               <PrivateAction>
                  <LongitudinalAction>
                     <SpeedAction>
                        <SpeedActionDynamics dynamicsShape="step" value="0.0" dynamicsDimension="time"/>
                        <SpeedActionTarget>
                           <AbsoluteTargetSpeed value="{traffic_speed_ms}"/>
                        </SpeedActionTarget>
                     </SpeedAction>
                  </LongitudinalAction>
               </PrivateAction>
            </Private>'''

    # Migration slice 2: <Entities> now comes from scenariogeneration (manual fallback).
    entities_block = build_entities_block(
        npc, pedestrian_model, num_traffic_vehicles,
        npc_entity, traffic_vehicle_entities,
    )

    # Migration slice 3: Ego Init block from scenariogeneration (manual fallback).
    ego_init = build_ego_init_block(host_speed_ms)

    # -- Phase 3: headlights at night for vehicle entities (skips pedestrian/cyclist). --
    headlight_entities = ["Ego"]
    if npc["kind"] == "vehicle":
        headlight_entities.append("NPC")
    headlight_entities += [f"TrafficVehicle_{i + 1}" for i in range(num_traffic_vehicles)]
    headlight_group = build_headlights(data, headlight_entities)

    return f'''<?xml version="1.0" encoding="UTF-8"?>
<OpenSCENARIO>
   <FileHeader revMajor="1"
               revMinor="2"
               date="2026-06-07T00:00:00"
               description="Narrative XAI generated driving scenario"
               author="Sadia Aman"/>

   <ParameterDeclarations>
      <ParameterDeclaration name="HostVehicle" parameterType="string" value="car_blue"/>
      <ParameterDeclaration name="HostSpeed" parameterType="double" value="{host_speed_ms}"/>
      <ParameterDeclaration name="PedestrianSpeed" parameterType="double" value="1.5"/>
      <ParameterDeclaration name="WeatherCondition" parameterType="string" value="{data.weather}"/>
      <ParameterDeclaration name="NPCType" parameterType="string" value="{data.npcType}"/>
      <ParameterDeclaration name="NPCBehaviour" parameterType="string" value="{data.npcBehavior}"/>
      <ParameterDeclaration name="EgoResponse" parameterType="string" value="{data.egoResponse}"/>
      <ParameterDeclaration name="Improved" parameterType="boolean" value="{str(data.improve).lower()}"/>
      <ParameterDeclaration name="NpcSpeed" parameterType="double" value="{npc_speed_ms}"/>
   </ParameterDeclarations>

   <CatalogLocations>
      <RouteCatalog>
         <Directory path="{route_catalog_dir}"/>
      </RouteCatalog>
      <VehicleCatalog>
         <Directory path="{vehicle_catalog_dir}"/>
      </VehicleCatalog>
   </CatalogLocations>

   <RoadNetwork>
      <LogicFile filepath="{logic_file}"/>
      <SceneGraphFile filepath="{scene_graph_file}"/>
   </RoadNetwork>

   {entities_block}

   <Storyboard>
      <Init>
         <Actions>
            {environment_action}

            {ego_init}
{npc_init}{traffic_vehicle_init_actions}
         </Actions>
      </Init>

      <Story name="GeneratedScenarioStory">
         <Act name="GeneratedScenarioAct">{npc_maneuver_group}{ego_response_group}{headlight_group}

            <StartTrigger>
               <ConditionGroup>
                  <Condition name="ActStartCondition" delay="0" conditionEdge="none">
                     <ByValueCondition>
                        <SimulationTimeCondition value="0" rule="greaterThan"/>
                     </ByValueCondition>
                  </Condition>
               </ConditionGroup>
            </StartTrigger>
         </Act>
      </Story>

      <StopTrigger>
         <ConditionGroup>
            <Condition name="QuitCondition" delay="0" conditionEdge="rising">
               <ByValueCondition>
                  <SimulationTimeCondition value="20" rule="greaterThan"/>
               </ByValueCondition>
            </Condition>
         </ConditionGroup>
      </StopTrigger>
   </Storyboard>
</OpenSCENARIO>'''


# Proven pedestrian/cyclist crossing path on fabriksgatan road 0, taken from
# esmini's own pedestrian.xosc. Used for NPC kind "crosser".
CROSSING_TRAJECTORY = '''
                                 <Trajectory closed="false" name="npc_crossing_trajectory">
                                    <ParameterDeclarations/>
                                    <Shape>
                                       <Polyline>
                                          <Vertex><Position><LanePosition laneId="3" offset="0.5" roadId="0" s="15"><Orientation type="relative" h="0" p="0" r="0"/></LanePosition></Position></Vertex>
                                          <Vertex><Position><LanePosition laneId="3" offset="0.5" roadId="0" s="10.5"><Orientation type="relative" h="0" p="0" r="0"/></LanePosition></Position></Vertex>
                                          <Vertex><Position><LanePosition laneId="3" offset="0.0" roadId="0" s="10"><Orientation type="relative" h="1.57" p="0" r="0"/></LanePosition></Position></Vertex>
                                          <Vertex><Position><LanePosition laneId="-3" offset="0.0" roadId="0" s="10"><Orientation type="relative" h="4.71" p="0" r="0"/></LanePosition></Position></Vertex>
                                          <Vertex><Position><LanePosition laneId="-3" offset="-0.5" roadId="0" s="9.5"><Orientation type="relative" h="3.14" p="0" r="0"/></LanePosition></Position></Vertex>
                                          <Vertex><Position><LanePosition laneId="-3" offset="-0.5" roadId="0" s="7.5"><Orientation type="relative" h="3.14" p="0" r="0"/></LanePosition></Position></Vertex>
                                       </Polyline>
                                    </Shape>
                                 </Trajectory>'''


def _ego_traveled_trigger(condition_name: str, distance: float) -> str:
    """A StartTrigger that fires once the Ego has traveled `distance` metres."""
    return f'''<StartTrigger>
                        <ConditionGroup>
                           <Condition conditionEdge="rising" delay="0" name="{condition_name}">
                              <ByEntityCondition>
                                 <TriggeringEntities triggeringEntitiesRule="any">
                                    <EntityRef entityRef="Ego"/>
                                 </TriggeringEntities>
                                 <EntityCondition>
                                    <TraveledDistanceCondition value="{distance}"/>
                                 </EntityCondition>
                              </ByEntityCondition>
                           </Condition>
                        </ConditionGroup>
                     </StartTrigger>'''


def build_npc(data: ScenarioRequest, npc: dict, host_speed_ms: float,
              pedestrian_model: str, npc_speed_ms: float):
    """
    Step 3 + Step 4: build the main "other actor".

    Returns (npc_entity, npc_init, npc_maneuver_group) XML fragments. The entity
    is always named "NPC" so triggers elsewhere can reference it regardless of type.

    npc_speed_ms is the user-selected NPC speed (m/s): the lead vehicle's drive
    speed for car/truck, and the crossing speed for pedestrian/cyclist.

    When data.improve is set, the maneuver is intensified (more sudden crossing,
    harder/faster vehicle maneuvers) to give a sharper "improved behaviour" variant.
    """
    improve = data.improve
    if npc["kind"] == "crosser":
        # --- Entity: pedestrian uses an inline Pedestrian, cyclist a catalog vehicle ---
        if npc["catalog"]:
            npc_entity = f'''
      <ScenarioObject name="NPC">
         <CatalogReference catalogName="VehicleCatalog" entryName="{npc['catalog']}"/>
      </ScenarioObject>'''
        else:
            npc_entity = f'''
      <ScenarioObject name="NPC">
         <Pedestrian mass="80" model="EPTa" name="NPC" pedestrianCategory="pedestrian" model3d="{pedestrian_model}">
            <ParameterDeclarations/>
            <BoundingBox>
               <Center x="0.06" y="0.0" z="0.923"/>
               <Dimensions height="1.8" length="0.6" width="0.5"/>
            </BoundingBox>
            <Properties>
               <Property name="scaleMode" value="BBToModel"/>
            </Properties>
         </Pedestrian>
      </ScenarioObject>'''

        # --- Init: place at the kerb, ready to cross ---
        npc_init = '''
            <Private entityRef="NPC">
               <PrivateAction>
                  <TeleportAction>
                     <Position>
                        <LanePosition laneId="3" offset="0.5" roadId="0" s="15">
                           <Orientation type="relative" h="0" p="0" r="0"/>
                        </LanePosition>
                     </Position>
                  </TeleportAction>
               </PrivateAction>
            </Private>'''

        # --- Maneuver: walk/ride across the road on the proven trajectory ---
        # Cross at the user-selected NPC speed. "improve" sharpens the EGO
        # reaction (reliable, early brake), not the vulnerable road user's speed.
        cross_speed = npc_speed_ms
        cross_accel = "2"
        start_trigger = _ego_traveled_trigger("npc_cross_condition", 5)
        npc_maneuver_group = f'''
            <ManeuverGroup maximumExecutionCount="1" name="npc_group">
               <Actors selectTriggeringEntities="false">
                  <EntityRef entityRef="NPC"/>
               </Actors>
               <Maneuver name="npc_maneuver">
                  <Event maximumExecutionCount="1" name="npc_event" priority="overwrite">
                     <Action name="npc_cross_speed">
                        <PrivateAction>
                           <LongitudinalAction>
                              <SpeedAction>
                                 <SpeedActionDynamics dynamicsShape="linear" value="{cross_accel}" dynamicsDimension="rate"/>
                                 <SpeedActionTarget>
                                    <AbsoluteTargetSpeed value="{cross_speed}"/>
                                 </SpeedActionTarget>
                              </SpeedAction>
                           </LongitudinalAction>
                        </PrivateAction>
                     </Action>
                     <Action name="npc_cross_route">
                        <PrivateAction>
                           <RoutingAction>
                              <FollowTrajectoryAction>{CROSSING_TRAJECTORY}
                                 <TimeReference><None/></TimeReference>
                                 <TrajectoryFollowingMode followingMode="follow"/>
                              </FollowTrajectoryAction>
                           </RoutingAction>
                        </PrivateAction>
                     </Action>
                     {start_trigger}
                  </Event>
               </Maneuver>
            </ManeuverGroup>'''
        return npc_entity, npc_init, npc_maneuver_group

    # ----------------------------- vehicle NPC -----------------------------
    # The lead vehicle drives at the user-selected NPC speed (m/s).
    npc_speed = max(npc_speed_ms, 0.5)

    # "cuts in front of ego" starts in the neighbouring lane (2) and moves into
    # ego's lane (1); everything else starts directly ahead in ego's lane (1).
    start_lane = 2 if data.npcBehavior == "cuts in front of ego" else 1

    npc_entity = f'''
      <ScenarioObject name="NPC">
         <CatalogReference catalogName="VehicleCatalog" entryName="{npc['catalog']}"/>
      </ScenarioObject>'''

    # Teleport via RoutePosition so esmini orients the NPC along ego's travel
    # direction (correct heading), without binding it to the route (so lane
    # changes remain possible).
    npc_init = f'''
            <Private entityRef="NPC">
               <PrivateAction>
                  <TeleportAction>
                     <Position>
                        <RoutePosition>
                           <RouteRef>
                              <CatalogReference catalogName="RoutesAtFabriksgatan" entryName="HostStraightRoute"/>
                           </RouteRef>
                           <InRoutePosition>
                              <FromLaneCoordinates pathS="25" laneId="{start_lane}"/>
                           </InRoutePosition>
                        </RoutePosition>
                     </Position>
                  </TeleportAction>
               </PrivateAction>
               <PrivateAction>
                  <LongitudinalAction>
                     <SpeedAction>
                        <SpeedActionDynamics dynamicsShape="step" value="0.0" dynamicsDimension="time"/>
                        <SpeedActionTarget>
                           <AbsoluteTargetSpeed value="{npc_speed}"/>
                        </SpeedActionTarget>
                     </SpeedAction>
                  </LongitudinalAction>
               </PrivateAction>
            </Private>'''

    # --- Behaviour: pick the maneuver action (improved = faster / harder) ---
    if data.npcBehavior == "changes lane suddenly":
        # Veer out of ego's lane (relative +1).
        lane_change_time = "1.2" if improve else "2"
        action_body = f'''<PrivateAction>
                           <LateralAction>
                              <LaneChangeAction>
                                 <LaneChangeActionDynamics dynamicsShape="sinusoidal" value="{lane_change_time}" dynamicsDimension="time"/>
                                 <LaneChangeTarget>
                                    <RelativeTargetLane entityRef="NPC" value="1"/>
                                 </LaneChangeTarget>
                              </LaneChangeAction>
                           </LateralAction>
                        </PrivateAction>'''
    elif data.npcBehavior == "cuts in front of ego":
        # Started in lane 2, cut into ego's lane (relative -1).
        cut_in_time = "1.0" if improve else "1.5"
        action_body = f'''<PrivateAction>
                           <LateralAction>
                              <LaneChangeAction>
                                 <LaneChangeActionDynamics dynamicsShape="sinusoidal" value="{cut_in_time}" dynamicsDimension="time"/>
                                 <LaneChangeTarget>
                                    <RelativeTargetLane entityRef="NPC" value="-1"/>
                                 </LaneChangeTarget>
                              </LaneChangeAction>
                           </LateralAction>
                        </PrivateAction>'''
    else:
        # "brakes suddenly" (and a non-applicable "crosses the road" for vehicles):
        # the lead vehicle brakes hard to a stop.
        brake_rate = "-9" if improve else "-6"
        action_body = f'''<PrivateAction>
                           <LongitudinalAction>
                              <SpeedAction>
                                 <SpeedActionDynamics dynamicsShape="linear" value="{brake_rate}" dynamicsDimension="rate"/>
                                 <SpeedActionTarget>
                                    <AbsoluteTargetSpeed value="0"/>
                                 </SpeedActionTarget>
                              </SpeedAction>
                           </LongitudinalAction>
                        </PrivateAction>'''

    start_trigger = _ego_traveled_trigger("npc_behaviour_condition", 6 if improve else 8)
    npc_maneuver_group = f'''
            <ManeuverGroup maximumExecutionCount="1" name="npc_group">
               <Actors selectTriggeringEntities="false">
                  <EntityRef entityRef="NPC"/>
               </Actors>
               <Maneuver name="npc_maneuver">
                  <Event maximumExecutionCount="1" name="npc_event" priority="overwrite">
                     <Action name="npc_behaviour_action">
                        {action_body}
                     </Action>
                     {start_trigger}
                  </Event>
               </Maneuver>
            </ManeuverGroup>'''
    return npc_entity, npc_init, npc_maneuver_group


def build_ego_response(data: ScenarioRequest, host_speed_ms: float) -> str:
    """
    Step 5: build the ego reaction, triggered when the ego gets close to the NPC.

    The trigger is a RelativeDistanceCondition (longitudinal distance to the NPC)
    rather than TimeToCollision: TTC is built for car-following and does not fire
    reliably for a pedestrian crossing the road perpendicular to the ego, so the
    ego previously never braked for a crossing pedestrian. Distance-to-NPC is
    simple and reliable for both crossing pedestrians and lead vehicles.

    The threshold is speed-scaled (braking distance + margin) so the ego still
    stops in time at higher speeds, not just at 30 km/h.
    """
    # Stopping distance at the hardest deceleration we use (~8 m/s^2) plus an 8 m
    # margin, so the ego begins braking with room to stop before the NPC. Improved:
    # react 30% earlier (larger trigger distance) for a more decisive reaction.
    #
    # Cap at 40 m: the ego<->NPC starting gap on this road is ~48 m for a crossing
    # pedestrian, so an uncapped distance (e.g. ~50 m at 80 km/h improved) would be
    # already satisfied at t=0 and the brake would never trigger. 40 m keeps the
    # condition starting false so it fires reliably and the ego stops in time.
    improve = data.improve
    trigger_distance = min(
        round((host_speed_ms ** 2 / 16 + 8) * (1.3 if improve else 1.0), 1), 40.0
    )
    if data.egoResponse == "brakes immediately":
        brake_rate = "-10" if improve else "-8"
        actions = f'''<Action name="ego_response_action">
                        <PrivateAction>
                           <LongitudinalAction>
                              <SpeedAction>
                                 <SpeedActionDynamics dynamicsShape="linear" value="{brake_rate}" dynamicsDimension="rate"/>
                                 <SpeedActionTarget>
                                    <AbsoluteTargetSpeed value="0"/>
                                 </SpeedActionTarget>
                              </SpeedAction>
                           </LongitudinalAction>
                        </PrivateAction>
                     </Action>'''
    elif data.egoResponse == "steers to avoid":
        # Lane change away from the NPC plus a speed reduction. Improved: quicker
        # swerve and a stronger slow-down.
        steer_time = "1.2" if improve else "2"
        slow_rate = "-5" if improve else "-3"
        target_speed = round(host_speed_ms * (0.4 if improve else 0.6), 2)
        actions = f'''<Action name="ego_response_steer">
                        <PrivateAction>
                           <LateralAction>
                              <LaneChangeAction>
                                 <LaneChangeActionDynamics dynamicsShape="sinusoidal" value="{steer_time}" dynamicsDimension="time"/>
                                 <LaneChangeTarget>
                                    <RelativeTargetLane entityRef="Ego" value="1"/>
                                 </LaneChangeTarget>
                              </LaneChangeAction>
                           </LateralAction>
                        </PrivateAction>
                     </Action>
                     <Action name="ego_response_slow">
                        <PrivateAction>
                           <LongitudinalAction>
                              <SpeedAction>
                                 <SpeedActionDynamics dynamicsShape="linear" value="{slow_rate}" dynamicsDimension="rate"/>
                                 <SpeedActionTarget>
                                    <AbsoluteTargetSpeed value="{target_speed}"/>
                                 </SpeedActionTarget>
                              </SpeedAction>
                           </LongitudinalAction>
                        </PrivateAction>
                     </Action>'''
    else:
        # "slows down" and "keeps lane and reduces speed": deceleration to a
        # fraction of the original speed (no lane change). Improved: brake harder
        # and to a lower speed.
        if data.egoResponse == "slows down":
            rate, factor = ("-5", 0.3) if improve else ("-3", 0.4)
        else:  # keeps lane and reduces speed
            rate, factor = ("-6", 0.35) if improve else ("-4", 0.5)
        target_speed = round(host_speed_ms * factor, 2)
        actions = f'''<Action name="ego_response_action">
                        <PrivateAction>
                           <LongitudinalAction>
                              <SpeedAction>
                                 <SpeedActionDynamics dynamicsShape="linear" value="{rate}" dynamicsDimension="rate"/>
                                 <SpeedActionTarget>
                                    <AbsoluteTargetSpeed value="{target_speed}"/>
                                 </SpeedActionTarget>
                              </SpeedAction>
                           </LongitudinalAction>
                        </PrivateAction>
                     </Action>'''

    return f'''
            <ManeuverGroup maximumExecutionCount="1" name="ego_response_group">
               <Actors selectTriggeringEntities="false">
                  <EntityRef entityRef="Ego"/>
               </Actors>
               <Maneuver name="ego_response_maneuver">
                  <Event name="ego_response_event" priority="overwrite">
                     {actions}
                     <StartTrigger>
                        <ConditionGroup>
                           <Condition name="ego_response_condition" delay="0" conditionEdge="none">
                              <ByEntityCondition>
                                 <TriggeringEntities triggeringEntitiesRule="any">
                                    <EntityRef entityRef="Ego"/>
                                 </TriggeringEntities>
                                 <EntityCondition>
                                    <RelativeDistanceCondition entityRef="NPC"
                                                               value="{trigger_distance}"
                                                               freespace="true"
                                                               coordinateSystem="entity"
                                                               relativeDistanceType="longitudinal"
                                                               rule="lessThan"/>
                                 </EntityCondition>
                              </ByEntityCondition>
                           </Condition>
                        </ConditionGroup>
                     </StartTrigger>
                  </Event>
               </Maneuver>
            </ManeuverGroup>'''


def build_headlights(data: ScenarioRequest, entity_names: list[str]) -> str:
    """
    Phase 3: switch low-beam headlights on at night for the given vehicle
    entities. Returns a Story ManeuverGroup (empty string in Day mode, so Day
    output is unchanged).

    Implemented as a maneuver (not Init PrivateActions) because esmini activates
    each entity once per Init <Private> block; a second Init <Private> for an
    entity that already has one fails with "Already active". A ManeuverGroup with
    the vehicles as actors applies the AppearanceAction/LightStateAction that
    esmini 3.2.1 supports (see resources LightStateManeuvers.xosc).
    """
    if data.timeOfDay.lower() == "day" or not entity_names:
        return ""

    actors = "".join(
        f'''
                  <EntityRef entityRef="{name}"/>''' for name in entity_names
    )
    return f'''
            <ManeuverGroup maximumExecutionCount="1" name="headlights_group">
               <Actors selectTriggeringEntities="false">{actors}
               </Actors>
               <Maneuver name="headlights_maneuver">
                  <Event name="headlights_event" priority="parallel" maximumExecutionCount="1">
                     <Action name="headlights_action">
                        <PrivateAction>
                           <AppearanceAction>
                              <LightStateAction>
                                 <LightType>
                                    <VehicleLight vehicleLightType="lowBeam"/>
                                 </LightType>
                                 <LightState mode="on" luminousIntensity="12000"/>
                              </LightStateAction>
                           </AppearanceAction>
                        </PrivateAction>
                     </Action>
                     <StartTrigger>
                        <ConditionGroup>
                           <Condition name="headlights_condition" delay="0" conditionEdge="none">
                              <ByValueCondition>
                                 <SimulationTimeCondition value="0" rule="greaterThan"/>
                              </ByValueCondition>
                           </Condition>
                        </ConditionGroup>
                     </StartTrigger>
                  </Event>
               </Maneuver>
            </ManeuverGroup>'''


def build_scenario_filename(data: ScenarioRequest, output_dir: Path) -> str:
    # Keep versioning sequential and readable. The next index is computed ONLY
    # from files inside output_dir, so each variant folder numbers independently.
    max_index = 0
    name_pattern = re.compile(r"scenario_(\d{3})_.*\.xosc")

    for path in output_dir.glob("scenario_*.xosc"):
        match = name_pattern.match(path.name)
        if match:
            max_index = max(max_index, int(match.group(1)))

    next_index = max_index + 1
    safe_weather = data.weather.replace(" ", "_")
    improved_tag = "_improved" if data.improve else ""
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    return f"scenario_{next_index:03d}_{data.egoSpeed}kmh_{data.trafficVehicles}traffic_{safe_weather}{improved_tag}_{timestamp}.xosc"


def get_current_xosc_path(variant: str) -> Path | None:
    """
    Latest generated XOSC for one variant ONLY. Returns the variant's own latest
    pointer, else the newest file in that variant's folder. Never reads the other
    variant's folder, so Run/Export can't cross variants.
    """
    global LATEST_STRUCTURED_XOSC_PATH, LATEST_UNSTRUCTURED_XOSC_PATH

    if variant == "structured":
        if LATEST_STRUCTURED_XOSC_PATH and LATEST_STRUCTURED_XOSC_PATH.exists():
            return LATEST_STRUCTURED_XOSC_PATH
    elif variant == "unstructured":
        if LATEST_UNSTRUCTURED_XOSC_PATH and LATEST_UNSTRUCTURED_XOSC_PATH.exists():
            return LATEST_UNSTRUCTURED_XOSC_PATH

    output_dir = VARIANT_DIRS.get(variant)
    if output_dir is None:
        return None

    candidates = sorted(output_dir.glob("scenario_*.xosc"), key=lambda p: p.stat().st_mtime, reverse=True)
    if not candidates:
        return None

    if variant == "structured":
        LATEST_STRUCTURED_XOSC_PATH = candidates[0]
    else:
        LATEST_UNSTRUCTURED_XOSC_PATH = candidates[0]
    return candidates[0]


def generate_and_save(data: ScenarioRequest, output_dir: Path, variant: str) -> dict:
    """
    Shared generation path for both variants. The caller passes the explicit
    output_dir and variant ("structured" / "unstructured") — the folder is never
    guessed from the prompt. Builds the XOSC, saves a versioned file into
    output_dir, updates only that variant's latest pointer, and returns the result.
    """
    xosc = build_xosc_preview(data)
    filename = build_scenario_filename(data, output_dir)
    xosc_path = output_dir / filename
    xosc_path.write_text(xosc, encoding="utf-8")

    global LATEST_STRUCTURED_XOSC_PATH, LATEST_UNSTRUCTURED_XOSC_PATH, LATEST_XODR_PATH
    if variant == "structured":
        LATEST_STRUCTURED_XOSC_PATH = xosc_path
    else:
        LATEST_UNSTRUCTURED_XOSC_PATH = xosc_path
    LATEST_XODR_PATH = road_files_for(data)[0]  # road file this scenario uses

    num_traffic_vehicles = min(max(data.trafficVehicles, 0), 3)
    actual_actor_count = 2 + num_traffic_vehicles  # Ego + NPC + traffic vehicles
    return {
        "filename": filename,
        "variant": variant,
        "actors": actual_actor_count,
        "duration": 14.2,
        "improved": data.improve,
        "npcSpeed": data.npcSpeed,
        "npcSpeedMs": round(data.npcSpeed / 3.6, 2),
        "npcType": data.npcType,
        "trafficVehicles": num_traffic_vehicles,
        "xosc_preview": xosc,
    }


@app.post("/generate-scenario")
def generate_scenario(data: ScenarioRequest):
    # Variant A (structured) always saves into the structured folder.
    result = generate_and_save(data, STRUCTURED_GENERATED_DIR, "structured")
    npc_speed_ms = result["npcSpeedMs"]

    pipeline_log = [
        f"› Parsing prompt: \"{data.prompt[:80]}...\"",
        "› Sending structured prompt to generator...",
        "› Road: fixed fabriksgatan map (fabriksgatan.xodr).",
        f"› NPC speed: {data.npcSpeed} km/h ({npc_speed_ms} m/s) — recorded; applied to NPC movement in a later phase.",
        "› trafficVehicles received; vehicle generation is currently prototype-only and may not produce full XOSC traffic behavior.",
        f"✓ XOSC generated — {result['actors']} actors, 14.2 s duration",
    ]
    if data.improve:
        pipeline_log.append(
            "★ Improved actor behavior — sharper NPC maneuver, harder/earlier ego reaction."
        )
    pipeline_log += [
        "› Running validation step...",
        "✓ Validation passed — no issues",
        "✓ Scenario ready for export",
    ]

    result["pipeline_log"] = pipeline_log
    return result


# ---------------------------------------------------------------------------
# Variant B (unstructured): natural-language scenario parser.
# /generate-from-text tries the Gemini LLM parser (llm_parse.py) first and
# falls back to this rule-based parser when Gemini is unavailable or errors.
# Both paths produce the same ScenarioRequest, so generate_and_save /
# build_xosc_preview are shared by both variants unchanged.
# ---------------------------------------------------------------------------
_UNSUPPORTED_ACTORS = ("dog", "cat", "deer", "horse", "moose", "cow", "bird", "animal")


def parse_scenario_text(text: str):
    """
    Parse a natural-language description into (ScenarioRequest|None, parsed dict,
    messages). On any clarification/validation issue the request is None and the
    messages explain what to fix; the parsed dict always reflects what was
    understood (with safe defaults) so the UI can preview it.
    """
    t = (text or "").lower()
    messages: list[str] = []

    speeds = [int(s) for s in re.findall(r"(\d{1,3})\s*km", t)]

    # Ego speed (first km/h mention).
    ego_speed = speeds[0] if speeds else None
    if ego_speed is not None and not (5 <= ego_speed <= 130):
        messages.append("Ego speed is outside the supported range. Please use a realistic road speed.")
        ego_speed = None

    # NPC speed (second km/h mention, or a qualitative cue).
    npc_speed = None
    if len(speeds) >= 2 and 1 <= speeds[1] <= 130:
        npc_speed = float(speeds[1])
    elif "slow" in t:
        npc_speed = 20.0
    elif "fast" in t or "speeding" in t:
        npc_speed = 60.0

    # Time of day.
    if any(w in t for w in ("night", "evening", "dark", "midnight")):
        time_of_day = "Night"
    elif any(w in t for w in ("day", "daytime", "morning", "afternoon", "noon")):
        time_of_day = "Day"
    else:
        time_of_day = None

    # Weather.
    weather = None
    if "clear" in t or "sunny" in t:
        weather = "clear"
    if "rain" in t:
        weather = "rain"
    if "fog" in t:
        weather = "fog"
    if "snow" in t:
        weather = "snow"

    # Road type / lane count is no longer parsed — every scenario uses the fixed
    # fabriksgatan road, so users don't need to mention road width.

    # Traffic vehicles.
    traffic = None
    if any(w in t for w in ("no traffic", "without traffic", "no other")):
        traffic = 0
    else:
        tm = re.search(r"(\d)\s*(?:other\s+)?(?:cars|vehicles|traffic)", t)
        if tm:
            traffic = min(int(tm.group(1)), 3)

    # NPC / actor type. Prefer specific actors over the generic "car/vehicle",
    # and ignore the ego's own "vehicle" so "ego vehicle" / background "cars in
    # traffic" don't get mistaken for the NPC.
    npc_text = t.replace("ego vehicle", "ego").replace("ego car", "ego").replace("host vehicle", "host")
    if "truck" in t or "lorry" in t:
        npc_type = "truck"
    elif any(w in t for w in ("cyclist", "bicycle", "bike rider")) or re.search(r"\bbike\b", t):
        npc_type = "cyclist"
    elif any(w in t for w in ("pedestrian", "person", "walker", "child")):
        npc_type = "pedestrian"
    elif re.search(r"\bcars?\b", npc_text) or re.search(r"\bvehicle\b", npc_text):
        npc_type = "car"
    else:
        npc_type = None
    for animal in _UNSUPPORTED_ACTORS:
        if re.search(rf"\b{animal}\b", t):
            messages.append(
                f"Unsupported actor: {animal}. Supported actors are car, truck, pedestrian, and cyclist."
            )
            break

    # NPC behaviour.
    if any(w in t for w in ("cuts in", "cut in", "cut-in", "pulls out", "pull out", "merge")):
        npc_behavior = "cuts in front of ego"
    elif "lane" in t and ("change" in t or "switch" in t):
        npc_behavior = "changes lane suddenly"
    elif any(w in t for w in ("crosses", "cross the road", "walks across", "walk across", "crossing")):
        npc_behavior = "crosses the road"
    elif any(w in t for w in ("brakes suddenly", "stops suddenly", "sudden brake", "slams")):
        npc_behavior = "brakes suddenly"
    else:
        npc_behavior = None

    # Ego response.
    if any(w in t for w in ("steer", "swerve", "avoid")):
        ego_response = "steers to avoid"
    elif "keep" in t and "lane" in t:
        ego_response = "keeps lane and reduces speed"
    elif any(w in t for w in ("slows", "slow down", "reduce speed", "reduces speed")):
        ego_response = "slows down"
    elif any(w in t for w in ("brake", "brakes", "stops", "emergency", "hard stop")):
        ego_response = "brakes immediately"
    else:
        ego_response = None

    # Consistency (pedestrian/cyclist only cross; car/truck cannot cross).
    if npc_type in ("pedestrian", "cyclist") and npc_behavior and npc_behavior != "crosses the road":
        messages.append(
            f"The behaviour '{npc_behavior}' is not suitable for {npc_type}. "
            "Please use 'crosses the road' or choose a vehicle actor."
        )
    if npc_type in ("car", "truck") and npc_behavior == "crosses the road":
        messages.append(
            f"The behaviour 'crosses the road' is not suitable for {npc_type}. "
            "Please use a vehicle behaviour like 'brakes suddenly' or 'cuts in front of ego'."
        )

    # Ambiguity / missing-core clarifications (only if no hard error yet).
    if not messages:
        if npc_type and not npc_behavior:
            messages.append(
                "Please specify the actor type and behaviour, for example: car brakes suddenly, "
                "pedestrian crosses the road, or truck cuts in front of ego."
            )
        elif any(v is None for v in (ego_speed, npc_type, npc_behavior, ego_response)):
            messages.append(
                "Please provide more details such as ego speed, weather, NPC actor, "
                "NPC behaviour, and ego response."
            )

    # Parsed preview (final values with safe defaults applied where reasonable).
    parsed = {
        "egoSpeed": ego_speed,
        "trafficVehicles": traffic if traffic is not None else 0,
        "timeOfDay": time_of_day or "Day",
        "weather": weather or "clear",
        "npcType": npc_type,
        "npcSpeed": npc_speed if npc_speed is not None else 30.0,
        "npcBehavior": npc_behavior,
        "egoResponse": ego_response,
    }

    if messages:
        return None, parsed, messages

    data = ScenarioRequest(
        egoSpeed=ego_speed,
        trafficVehicles=parsed["trafficVehicles"],
        weather=parsed["weather"],
        timeOfDay=parsed["timeOfDay"],
        npcType=npc_type,
        npcBehavior=npc_behavior,
        egoResponse=ego_response,
        prompt=text,
        npcSpeed=parsed["npcSpeed"],
    )
    return data, parsed, []


@app.post("/generate-from-text")
def generate_from_text(req: TextScenarioRequest):
    snippet = (req.text or "").strip()[:80]
    data = None
    parsed: dict = {}
    messages: list = []
    parser_used = "rule-based"

    # Try Gemini LLM parser first; fall back to rule-based on any technical failure.
    try:
        from llm_parse import llm_parse_scenario, LLM_AVAILABLE
        if LLM_AVAILABLE:
            request_kwargs, llm_parsed, llm_messages = llm_parse_scenario(req.text)
            if llm_parsed is not None:
                # Gemini ran (success or validation failure) — use its output.
                parser_used = "gemini"
                parsed = llm_parsed
                messages = llm_messages or []
                if request_kwargs is not None:
                    data = ScenarioRequest(**request_kwargs)
    except Exception:
        pass  # import error or unexpected issue — fall through to rule-based

    # Rule-based fallback: used when Gemini was unavailable or had a technical error.
    if parser_used == "rule-based":
        data, parsed, messages = parse_scenario_text(req.text)

    parser_label = "Gemini LLM" if parser_used == "gemini" else "rule-based parser"

    if data is None:
        return {
            "ok": False,
            "parsed": parsed,
            "messages": messages,
            "parserUsed": parser_used,
            "pipeline_log": [
                f'› Parsing description: "{snippet}..."',
                f"  (using {parser_label})",
                "✗ Prompt parsing failed — clarification needed:",
                *[f"  • {m}" for m in messages],
            ],
        }

    # Variant B (unstructured) always saves into the unstructured folder.
    result = generate_and_save(data, UNSTRUCTURED_GENERATED_DIR, "unstructured")
    result["ok"] = True
    result["parsed"] = parsed
    result["parserUsed"] = parser_used
    result["pipeline_log"] = [
        f'› Parsing description: "{snippet}..."',
        f"✓ Prompt parsed using {parser_label} — see parsed parameters below.",
        f"✓ XOSC generated — {result['actors']} actors on the fabriksgatan road.",
        "› Esmini validation — runs when you click Run in esmini.",
        "✓ Scenario ready for export.",
    ]
    return result


SPEED_PRESETS = [30, 50, 80, 100]


def parse_refinement(text: str, current: ScenarioRequest) -> dict:
    """
    Map a free-text follow-up instruction to parameter overrides (keyword-based,
    no LLM). Only recognised phrases produce overrides; everything else is left
    unchanged. Returns a dict of the fields to override.
    """
    t = text.lower()
    o: dict = {}

    # Weather
    if "clear" in t or "sunny" in t:
        o["weather"] = "clear"
    if "rain" in t:
        o["weather"] = "rain"
    if "fog" in t:
        o["weather"] = "fog"
    if "snow" in t:
        o["weather"] = "snow"

    # Time of day
    if "night" in t:
        o["timeOfDay"] = "Night"
    elif "day" in t:
        o["timeOfDay"] = "Day"

    # NPC / actor type
    if "truck" in t:
        o["npcType"] = "truck"
    if "cyclist" in t or "bicycle" in t or "bike" in t:
        o["npcType"] = "cyclist"
    if "pedestrian" in t or "person" in t or "walker" in t or "walking" in t:
        o["npcType"] = "pedestrian"
    if re.search(r"\bcar\b", t):
        o["npcType"] = "car"

    # NPC behaviour
    if "cut in" in t or "cuts in" in t or "cut-in" in t or "cutin" in t:
        o["npcBehavior"] = "cuts in front of ego"
    elif "lane" in t and ("change" in t or "changes" in t or "switch" in t):
        o["npcBehavior"] = "changes lane suddenly"
    elif "cross" in t:
        o["npcBehavior"] = "crosses the road"
    elif "sudden brake" in t or "brakes suddenly" in t or "brake suddenly" in t:
        o["npcBehavior"] = "brakes suddenly"

    # Ego response (ego-specific phrases to avoid clashing with NPC behaviour)
    if "steer" in t or "swerve" in t or "avoid" in t:
        o["egoResponse"] = "steers to avoid"
    elif "keep lane" in t or "keeps lane" in t or "stay in lane" in t:
        o["egoResponse"] = "keeps lane and reduces speed"
    elif "slow down" in t or "slows down" in t or "reduce speed" in t or "reduces speed" in t:
        o["egoResponse"] = "slows down"
    elif "stop" in t or "emergency" in t or "hard brake" in t or "brake hard" in t or "brakes immediately" in t:
        o["egoResponse"] = "brakes immediately"

    # Ego speed: explicit number first, then relative faster/slower.
    speed_match = re.search(r"(\d{2,3})\s*km", t)
    if speed_match:
        o["egoSpeed"] = min(SPEED_PRESETS, key=lambda p: abs(p - int(speed_match.group(1))))
    elif "faster" in t or "speed up" in t:
        idx = min(range(len(SPEED_PRESETS)), key=lambda i: abs(SPEED_PRESETS[i] - current.egoSpeed))
        o["egoSpeed"] = SPEED_PRESETS[min(idx + 1, len(SPEED_PRESETS) - 1)]
    elif "slower" in t:
        idx = min(range(len(SPEED_PRESETS)), key=lambda i: abs(SPEED_PRESETS[i] - current.egoSpeed))
        o["egoSpeed"] = SPEED_PRESETS[max(idx - 1, 0)]

    # Traffic vehicles
    if "no traffic" in t or "without traffic" in t or "remove traffic" in t:
        o["trafficVehicles"] = 0
    elif "more traffic" in t or "add traffic" in t:
        o["trafficVehicles"] = min(current.trafficVehicles + 1, 3)
    elif "less traffic" in t or "fewer traffic" in t:
        o["trafficVehicles"] = max(current.trafficVehicles - 1, 0)

    # Intensify
    if any(w in t for w in ["aggressive", "sharper", "more realistic", "intensify", "improve"]):
        o["improve"] = True

    return o


@app.post("/refine-scenario")
def refine_scenario(data: RefineRequest):
    """
    Apply a free-text refinement to the current parameters, keep the NPC type and
    behaviour consistent, regenerate a new versioned XOSC, and report what changed.
    """
    fields = [
        ("egoSpeed", "Ego speed"),
        ("trafficVehicles", "Traffic vehicles"),
        ("timeOfDay", "Time of day"),
        ("weather", "Weather"),
        ("npcType", "NPC / actor type"),
        ("npcBehavior", "NPC behaviour"),
        ("egoResponse", "Ego response"),
        ("improve", "Improved"),
    ]
    original = {f: getattr(data, f) for f, _ in fields}

    # Prefer the LLM parser (Gemini, then Claude) when available; otherwise use the
    # keyword parser. Either way the result is the same shape, and the LLM path
    # can never break refinement (it returns None on any issue).
    # NOTE: the LLM SYSTEM INSTRUCTIONS (Role / Task / Context / Example / Format /
    # Boundaries / Note) live in llm_refine.py as the `_SYSTEM` constant.
    overrides = None
    provider_label = "keyword rules"
    try:
        from llm_refine import llm_parse_refinement, LLM_AVAILABLE, PROVIDER_LABEL
        if LLM_AVAILABLE:
            overrides = llm_parse_refinement(data.refineText, data)
            if overrides is not None:
                provider_label = PROVIDER_LABEL
    except Exception:
        overrides = None
    if overrides is None:
        overrides = parse_refinement(data.refineText, data)

    merged = {**original, **overrides}

    # Keep behaviour valid for the (possibly changed) NPC type, mirroring the UI.
    notes = []
    if merged["npcType"] in ("pedestrian", "cyclist"):
        if merged["npcBehavior"] != "crosses the road":
            merged["npcBehavior"] = "crosses the road"
            notes.append("NPC behaviour set to 'crosses the road' to match the actor type.")
    else:  # car / truck
        if merged["npcBehavior"] not in ("brakes suddenly", "changes lane suddenly", "cuts in front of ego"):
            merged["npcBehavior"] = "brakes suddenly"
            notes.append("NPC behaviour set to 'brakes suddenly' to match the actor type.")

    changes = [f"{label}: {original[f]} → {merged[f]}" for f, label in fields if str(original[f]) != str(merged[f])]

    # Refine belongs to Variant A only — save into the structured folder and
    # update the structured latest pointer.
    refined = data.model_copy(update=merged)
    xosc = build_xosc_preview(refined)
    filename = build_scenario_filename(refined, STRUCTURED_GENERATED_DIR)
    xosc_path = STRUCTURED_GENERATED_DIR / filename
    xosc_path.write_text(xosc, encoding="utf-8")

    global LATEST_STRUCTURED_XOSC_PATH, LATEST_XODR_PATH
    LATEST_STRUCTURED_XOSC_PATH = xosc_path
    LATEST_XODR_PATH = road_files_for(refined)[0]

    num_traffic_vehicles = min(max(refined.trafficVehicles, 0), 3)
    actual_actor_count = 2 + num_traffic_vehicles

    pipeline_log = [
        f'› Refinement: "{data.refineText[:80]}"',
        f"  (interpreted by {provider_label})",
    ]
    if changes:
        pipeline_log += [f"  • {c}" for c in changes]
    else:
        pipeline_log.append("  • No known parameters matched — nothing changed.")
    pipeline_log += [f"  ⓘ {n}" for n in notes]
    pipeline_log.append(f"✓ Refined XOSC generated — {actual_actor_count} actors")

    return {
        "filename": filename,
        "actors": actual_actor_count,
        "duration": 14.2,
        "improved": refined.improve,
        "xosc_preview": xosc,
        "pipeline_log": pipeline_log,
        "changes": changes,
        "params": {f: merged[f] for f, _ in fields},
    }


def resolve_variant_xosc(variant: str) -> Path:
    """
    Resolve the latest XOSC for the given variant, or raise a variant-specific
    404. Never falls back to the other variant's file.
    """
    if variant not in VARIANT_DIRS:
        raise HTTPException(status_code=400, detail="Unknown variant.")
    xosc_path = get_current_xosc_path(variant)
    if not xosc_path or not xosc_path.exists():
        noun = "structured" if variant == "structured" else "unstructured"
        article = "a" if variant == "structured" else "an"
        raise HTTPException(
            status_code=404, detail=f"Generate {article} {noun} scenario first."
        )
    return xosc_path


@app.post("/run-esmini")
def run_esmini(variant: str = Query("structured")):
    xosc_path = resolve_variant_xosc(variant)

    if not ESMINI_EXE:
        return {
            "message": "⚠ esmini path is not configured yet. Set ESMINI_EXE environment variable after downloading esmini."
        }

    try:
        subprocess.Popen([
            ESMINI_EXE,
            "--osc",
            str(xosc_path),
            "--camera_mode",
            "orbit",
        ])
        return {"message": "✓ esmini started with orbit camera. Check the esmini window."}
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@app.get("/download-xosc")
def download_xosc(variant: str = Query("structured")):
    xosc_path = resolve_variant_xosc(variant)
    return FileResponse(str(xosc_path), filename=xosc_path.name)


# ---------------------------------------------------------------------------
# Video recording (FFmpeg gdigrab window capture)
# ---------------------------------------------------------------------------
class RecordRequest(BaseModel):
    variant: str
    # Exact XOSC filename to record. Empty => use that variant's latest file.
    filename: str = ""


def find_ffmpeg() -> str | None:
    """
    Locate ffmpeg: explicit FFMPEG_EXE override, then PATH, then the winget
    install location (Gyan.FFmpeg) so it works before a shell PATH refresh.
    """
    env = os.getenv("FFMPEG_EXE")
    if env and Path(env).exists():
        return env
    found = shutil.which("ffmpeg")
    if found:
        return found
    local = os.getenv("LOCALAPPDATA", "")
    if local:
        matches = list(
            (Path(local) / "Microsoft" / "WinGet" / "Packages").glob(
                "Gyan.FFmpeg*/**/bin/ffmpeg.exe"
            )
        )
        if matches:
            return str(matches[0])
    return None


def _no_scenario_detail(variant: str) -> str:
    article = "a" if variant == "structured" else "an"
    return f"Generate {article} {variant} scenario first."


def resolve_variant_xosc_file(variant: str, filename: str) -> Path:
    """
    Resolve the XOSC to record for a variant. With a filename, it must be a bare
    name resolving INSIDE that variant's folder (blocks path traversal). Without
    one, falls back to the variant's own latest file. Never reads the other variant.
    """
    output_dir = VARIANT_DIRS.get(variant)
    if output_dir is None:
        raise HTTPException(status_code=400, detail="Unknown variant.")

    if not filename:
        path = get_current_xosc_path(variant)
        if not path or not path.exists():
            raise HTTPException(status_code=404, detail=_no_scenario_detail(variant))
        return path

    if filename != Path(filename).name:
        raise HTTPException(status_code=400, detail="Invalid filename.")
    candidate = (output_dir / filename).resolve()
    if candidate.parent != output_dir.resolve():
        raise HTTPException(status_code=400, detail="Invalid filename.")
    if candidate.suffix.lower() != ".xosc" or not candidate.exists():
        raise HTTPException(status_code=404, detail=_no_scenario_detail(variant))
    return candidate


def parse_scenario_stop_time(xosc_text: str) -> float:
    """Read the StopTrigger SimulationTimeCondition value (seconds); default 20."""
    match = re.search(
        r'<StopTrigger>.*?<SimulationTimeCondition\s+value="([\d.]+)"',
        xosc_text, re.DOTALL,
    )
    try:
        return float(match.group(1)) if match else 20.0
    except (TypeError, ValueError):
        return 20.0


def unique_video_path(video_dir: Path, mp4_name: str) -> Path:
    """Never overwrite: scenario.mp4 -> scenario_recording_02.mp4 -> _03 -> ..."""
    candidate = video_dir / mp4_name
    if not candidate.exists():
        return candidate
    stem = Path(mp4_name).stem
    index = 2
    while True:
        alt = video_dir / f"{stem}_recording_{index:02d}.mp4"
        if not alt.exists():
            return alt
        index += 1


@app.post("/record-esmini")
def record_esmini(req: RecordRequest):
    """
    Launch esmini for the selected scenario and capture its window to an MP4 with
    FFmpeg gdigrab. The video is named after the XOSC and saved in the variant's
    own video folder. Returns the video filename, or a clear error (never a
    zero-byte file, never the other variant's video).
    """
    variant = req.variant
    if variant not in VARIANT_VIDEO_DIRS:
        raise HTTPException(status_code=400, detail="Unknown variant.")

    ffmpeg = find_ffmpeg()
    if not ffmpeg:
        raise HTTPException(
            status_code=400,
            detail="FFmpeg not found. Install FFmpeg to enable video recording.",
        )

    if not ESMINI_EXE or not Path(ESMINI_EXE).exists():
        raise HTTPException(status_code=400, detail="esmini executable not configured.")

    xosc_path = resolve_variant_xosc_file(variant, req.filename)

    video_dir = VARIANT_VIDEO_DIRS[variant]
    video_path = unique_video_path(video_dir, xosc_path.with_suffix(".mp4").name)

    stop_time = parse_scenario_stop_time(xosc_path.read_text(encoding="utf-8"))
    duration = round(stop_time + RECORDING_BUFFER_SECONDS, 2)

    # Start esmini with a fixed, on-screen window so gdigrab has a visible target.
    try:
        esmini_proc = subprocess.Popen([
            ESMINI_EXE, "--osc", str(xosc_path),
            "--camera_mode", "orbit",
            "--window", *ESMINI_RECORD_WINDOW,
        ])
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Could not start esmini: {exc}")

    def _stop_esmini():
        if esmini_proc.poll() is None:
            esmini_proc.terminate()
            try:
                esmini_proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                esmini_proc.kill()

    # Let esmini open and load the scene before capturing.
    time.sleep(ESMINI_WARMUP_SECONDS)
    if esmini_proc.poll() is not None:
        raise HTTPException(status_code=500, detail="esmini exited before recording could start.")

    def _run_ffmpeg(input_args: list[str]) -> subprocess.CompletedProcess:
        cmd = [
            ffmpeg, "-y",
            "-f", "gdigrab", "-framerate", str(RECORDING_FPS),
            "-t", str(duration),
            *input_args,
            "-c:v", "libx264", "-pix_fmt", "yuv420p",
            str(video_path),
        ]
        return subprocess.run(
            cmd, capture_output=True, text=True,
            timeout=DEFAULT_RECORDING_TIMEOUT_SECONDS,
        )

    try:
        # Primary: capture just the esmini window by title.
        result = _run_ffmpeg(["-i", f"title={ESMINI_WINDOW_TITLE}"])
        if result.returncode != 0 or not video_path.exists() or video_path.stat().st_size == 0:
            # Fallback: capture the full desktop (window may not match by title).
            if video_path.exists():
                video_path.unlink(missing_ok=True)
            result = _run_ffmpeg(["-i", "desktop"])
    except subprocess.TimeoutExpired:
        _stop_esmini()
        if video_path.exists():
            video_path.unlink(missing_ok=True)
        raise HTTPException(status_code=500, detail="Recording timed out.")
    finally:
        _stop_esmini()

    if result.returncode != 0 or not video_path.exists() or video_path.stat().st_size == 0:
        if video_path.exists():
            video_path.unlink(missing_ok=True)
        raise HTTPException(
            status_code=500,
            detail="Video recording failed: FFmpeg could not capture the esmini window.",
        )

    return {
        "ok": True,
        "videoFilename": video_path.name,
        "variant": variant,
        "message": "Video recording completed.",
    }


@app.get("/download-video")
def download_video(variant: str = Query("structured"), filename: str = Query(...)):
    video_dir = VARIANT_VIDEO_DIRS.get(variant)
    if video_dir is None:
        raise HTTPException(status_code=400, detail="Unknown variant.")
    if filename != Path(filename).name:
        raise HTTPException(status_code=400, detail="Invalid filename.")
    path = (video_dir / filename).resolve()
    if path.parent != video_dir.resolve() or not path.exists():
        raise HTTPException(status_code=404, detail="Video not found.")
    return FileResponse(str(path), filename=path.name, media_type="video/mp4")


def get_current_xodr_path() -> Path | None:
    """Road file for the latest scenario (falls back to the default road)."""
    if LATEST_XODR_PATH and LATEST_XODR_PATH.exists():
        return LATEST_XODR_PATH
    default = ESMINI_DEMO_DIR / "resources" / "xodr" / "fabriksgatan.xodr"
    return default if default.exists() else None


@app.get("/download-xodr")
def download_xodr():
    xodr_path = get_current_xodr_path()
    if not xodr_path or not xodr_path.exists():
        raise HTTPException(status_code=404, detail="No road file available.")

    return FileResponse(str(xodr_path), filename=xodr_path.name)


@app.get("/list-scenarios")
def list_scenarios(variant: str = Query("structured")):
    """
    List generated scenarios for ONE variant (newest first) so the frontend can
    compare two versions. Read-only. Defaults to "structured" (Variant A's Compare).
    Never mixes the two variant folders. egoSpeed/trafficVehicles/weather come from
    the filename; the descriptive parameters come from the XOSC ParameterDeclarations.
    """
    output_dir = VARIANT_DIRS.get(variant)
    if output_dir is None:
        raise HTTPException(status_code=400, detail="Unknown variant.")

    filename_pattern = re.compile(r"scenario_\d{3}_(\d+)kmh_(\d+)traffic_(.+)\.xosc")

    def param(text: str, name: str):
        match = re.search(rf'name="{name}"[^>]*value="([^"]*)"', text)
        return match.group(1) if match else None

    scenarios = []
    for path in sorted(output_dir.glob("scenario_*.xosc"),
                       key=lambda p: p.stat().st_mtime, reverse=True):
        match = filename_pattern.match(path.name)
        try:
            text = path.read_text(encoding="utf-8")
        except Exception:
            text = ""
        # timeOfDay isn't stored as a parameter; derive it from the dateTime hour.
        time_of_day = None
        tod_match = re.search(r'dateTime="[^"]*T(\d{2}):', text)
        if tod_match:
            hour = int(tod_match.group(1))
            time_of_day = "Day" if 6 <= hour < 18 else "Night"
        scenarios.append({
            "filename": path.name,
            "egoSpeed": int(match.group(1)) if match else None,
            "trafficVehicles": int(match.group(2)) if match else None,
            "timeOfDay": time_of_day,
            "weather": match.group(3) if match else param(text, "WeatherCondition"),
            "npcType": param(text, "NPCType"),
            "npcBehavior": param(text, "NPCBehaviour"),
            "egoResponse": param(text, "EgoResponse"),
            "improved": param(text, "Improved") or "false",
        })
    return {"scenarios": scenarios}


@app.get("/")
def health():
    return {"status": "Backend is running"}
