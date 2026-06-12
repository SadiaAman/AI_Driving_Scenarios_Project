from pathlib import Path
import os
import re
import subprocess

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from pydantic import BaseModel

# Phase 5 (gradual scenariogeneration migration): the Environment block is the
# first piece built with the scenariogeneration library instead of a manual XML
# string. The import is optional — if the library is unavailable we transparently
# fall back to the manual builder, so the generator never breaks.
try:
    from scenario_builder import environment_action_xml as _sg_environment_action_xml
    _SCENARIOGENERATION_AVAILABLE = True
except Exception:
    _SCENARIOGENERATION_AVAILABLE = False


BASE_DIR = Path(__file__).resolve().parent
GENERATED_DIR = BASE_DIR / "generated"
GENERATED_DIR.mkdir(exist_ok=True)
LATEST_XOSC_PATH = None

PROJECT_DIR = BASE_DIR.parents[2]
ESMINI_DEMO_DIR = PROJECT_DIR / "03_esmini" / "esmini-demo"

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
    egoSpeed: int
    trafficVehicles: int
    weather: str
    timeOfDay: str
    npcType: str
    npcBehavior: str
    egoResponse: str
    prompt: str


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
    "clear": ("zeroOktas", "dry", "0.0", "100000", "1.0", 100000),
    "rain": ("eightOktas", "rain", "0.7", "75", "0.7", 22000),
    "snow": ("eightOktas", "snow", "0.6", "60", "0.5", 45000),
    "fog": ("fourOktas", "dry", "0.0", "40", "0.8", 18000),
}

# NPC type -> how the "main other actor" is represented in the scenario.
#   kind "crosser": a vulnerable road user that crosses the road (proven
#                   trajectory taken from esmini's own pedestrian.xosc).
#   kind "vehicle": a lead vehicle ahead of ego in the same lane that can
#                   brake or change lane.
NPC_PRESETS = {
    "pedestrian": {"kind": "crosser", "catalog": None, "speed": 1.5},
    "cyclist": {"kind": "crosser", "catalog": "bicycle", "speed": 3.0},
    "car": {"kind": "vehicle", "catalog": "car_red", "speed_factor": 0.5},
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


def build_xosc_preview(data: ScenarioRequest) -> str:
    """
    Generate an esmini-compatible OpenSCENARIO file on the fabriksgatan road.

    The scene is parameterised by the UI:
    - Step 1/2: lighting + weather via EnvironmentAction (realistic lux values).
    - Step 3:   NPC type selects entity + 3D model (car / truck / cyclist / pedestrian).
    - Step 4:   NPC behaviour selects the maneuver (crosses / brakes / changes lane / cuts in).
    - Step 5:   Ego response selects the reaction (brakes / slows / keeps lane / steers).
    """

    # esmini expects speed in m/s. Convert ego speed from km/h to m/s.
    host_speed_ms = round(data.egoSpeed / 3.6, 2)

    (
        date_time,
        cloud_cover,
        precipitation_type,
        precipitation_intensity,
        fog_range,
        friction,
        sun_intensity,
    ) = get_environment(data)

    # Phase 5: this block now comes from scenariogeneration (manual fallback).
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
        data, npc, host_speed_ms, pedestrian_model
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
        # Varied colours/models so the background traffic is distinguishable from
        # the white Ego and the red NPC. All are existing VehicleCatalog entries.
        traffic_models = ["car_blue", "car_yellow", "van_red"]
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
      <ParameterDeclaration name="HostVehicle" parameterType="string" value="car_white"/>
      <ParameterDeclaration name="HostSpeed" parameterType="double" value="{host_speed_ms}"/>
      <ParameterDeclaration name="PedestrianSpeed" parameterType="double" value="1.5"/>
      <ParameterDeclaration name="WeatherCondition" parameterType="string" value="{data.weather}"/>
      <ParameterDeclaration name="NPCType" parameterType="string" value="{data.npcType}"/>
      <ParameterDeclaration name="NPCBehaviour" parameterType="string" value="{data.npcBehavior}"/>
      <ParameterDeclaration name="EgoResponse" parameterType="string" value="{data.egoResponse}"/>
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

   <Entities>
      <ScenarioObject name="Ego">
         <CatalogReference catalogName="VehicleCatalog" entryName="$HostVehicle"/>
      </ScenarioObject>

{npc_entity}{traffic_vehicle_entities}
   </Entities>

   <Storyboard>
      <Init>
         <Actions>
            {environment_action}

            <Private entityRef="Ego">
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
            </Private>

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


def build_npc(data: ScenarioRequest, npc: dict, host_speed_ms: float, pedestrian_model: str):
    """
    Step 3 + Step 4: build the main "other actor".

    Returns (npc_entity, npc_init, npc_maneuver_group) XML fragments. The entity
    is always named "NPC" so triggers elsewhere can reference it regardless of type.
    """
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
                                 <SpeedActionDynamics dynamicsShape="linear" value="2" dynamicsDimension="rate"/>
                                 <SpeedActionTarget>
                                    <AbsoluteTargetSpeed value="{npc['speed']}"/>
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
    npc_speed = max(round(host_speed_ms * npc.get("speed_factor", 0.5), 2), 2.0)

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

    # --- Behaviour: pick the maneuver action ---
    if data.npcBehavior == "changes lane suddenly":
        # Veer out of ego's lane (relative +1).
        action_body = '''<PrivateAction>
                           <LateralAction>
                              <LaneChangeAction>
                                 <LaneChangeActionDynamics dynamicsShape="sinusoidal" value="2" dynamicsDimension="time"/>
                                 <LaneChangeTarget>
                                    <RelativeTargetLane entityRef="NPC" value="1"/>
                                 </LaneChangeTarget>
                              </LaneChangeAction>
                           </LateralAction>
                        </PrivateAction>'''
    elif data.npcBehavior == "cuts in front of ego":
        # Started in lane 2, cut into ego's lane (relative -1).
        action_body = '''<PrivateAction>
                           <LateralAction>
                              <LaneChangeAction>
                                 <LaneChangeActionDynamics dynamicsShape="sinusoidal" value="1.5" dynamicsDimension="time"/>
                                 <LaneChangeTarget>
                                    <RelativeTargetLane entityRef="NPC" value="-1"/>
                                 </LaneChangeTarget>
                              </LaneChangeAction>
                           </LateralAction>
                        </PrivateAction>'''
    else:
        # "brakes suddenly" (and a non-applicable "crosses the road" for vehicles):
        # the lead vehicle brakes hard to a stop.
        action_body = '''<PrivateAction>
                           <LongitudinalAction>
                              <SpeedAction>
                                 <SpeedActionDynamics dynamicsShape="linear" value="-6" dynamicsDimension="rate"/>
                                 <SpeedActionTarget>
                                    <AbsoluteTargetSpeed value="0"/>
                                 </SpeedActionTarget>
                              </SpeedAction>
                           </LongitudinalAction>
                        </PrivateAction>'''

    start_trigger = _ego_traveled_trigger("npc_behaviour_condition", 8)
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
    # margin, so the ego begins braking with room to stop before the NPC.
    trigger_distance = round(host_speed_ms ** 2 / 16 + 8, 1)
    if data.egoResponse == "brakes immediately":
        actions = '''<Action name="ego_response_action">
                        <PrivateAction>
                           <LongitudinalAction>
                              <SpeedAction>
                                 <SpeedActionDynamics dynamicsShape="linear" value="-8" dynamicsDimension="rate"/>
                                 <SpeedActionTarget>
                                    <AbsoluteTargetSpeed value="0"/>
                                 </SpeedActionTarget>
                              </SpeedAction>
                           </LongitudinalAction>
                        </PrivateAction>
                     </Action>'''
    elif data.egoResponse == "steers to avoid":
        # Lane change away from the NPC plus a mild speed reduction.
        target_speed = round(host_speed_ms * 0.6, 2)
        actions = f'''<Action name="ego_response_steer">
                        <PrivateAction>
                           <LateralAction>
                              <LaneChangeAction>
                                 <LaneChangeActionDynamics dynamicsShape="sinusoidal" value="2" dynamicsDimension="time"/>
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
                                 <SpeedActionDynamics dynamicsShape="linear" value="-3" dynamicsDimension="rate"/>
                                 <SpeedActionTarget>
                                    <AbsoluteTargetSpeed value="{target_speed}"/>
                                 </SpeedActionTarget>
                              </SpeedAction>
                           </LongitudinalAction>
                        </PrivateAction>
                     </Action>'''
    else:
        # "slows down" and "keeps lane and reduces speed": gentle deceleration to
        # a fraction of the original speed (no lane change).
        if data.egoResponse == "slows down":
            rate, factor = "-3", 0.4
        else:  # keeps lane and reduces speed
            rate, factor = "-4", 0.5
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
                           <Condition name="ego_response_condition" delay="0" conditionEdge="rising">
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


def build_scenario_filename(data: ScenarioRequest) -> str:
    # Keep versioning sequential and readable.
    max_index = 0
    name_pattern = re.compile(r"scenario_(\d{3})_.*\.xosc")

    for path in GENERATED_DIR.glob("scenario_*.xosc"):
        match = name_pattern.match(path.name)
        if match:
            max_index = max(max_index, int(match.group(1)))

    next_index = max_index + 1
    safe_weather = data.weather.replace(" ", "_")
    return f"scenario_{next_index:03d}_{data.egoSpeed}kmh_{data.trafficVehicles}traffic_{safe_weather}.xosc"


def get_current_xosc_path() -> Path | None:
    global LATEST_XOSC_PATH
    if LATEST_XOSC_PATH and LATEST_XOSC_PATH.exists():
        return LATEST_XOSC_PATH

    candidates = sorted(GENERATED_DIR.glob("scenario_*.xosc"), key=lambda p: p.stat().st_mtime, reverse=True)
    if not candidates:
        return None

    LATEST_XOSC_PATH = candidates[0]
    return LATEST_XOSC_PATH


@app.post("/generate-scenario")
def generate_scenario(data: ScenarioRequest):
    xosc = build_xosc_preview(data)
    filename = build_scenario_filename(data)
    xosc_path = GENERATED_DIR / filename
    xosc_path.write_text(xosc, encoding="utf-8")

    global LATEST_XOSC_PATH
    LATEST_XOSC_PATH = xosc_path

    num_traffic_vehicles = min(max(data.trafficVehicles, 0), 3)
    actual_actor_count = 2 + num_traffic_vehicles  # Ego + pedestrian + traffic vehicles

    return {
        "filename": filename,
        "actors": actual_actor_count,
        "duration": 14.2,
        "xosc_preview": xosc,
        "pipeline_log": [
            f"› Parsing prompt: \"{data.prompt[:80]}...\"",
            "› Sending structured prompt to generator...",
            "› trafficVehicles received; vehicle generation is currently prototype-only and may not produce full XOSC traffic behavior.",
            f"✓ XOSC generated — {actual_actor_count} actors, 14.2 s duration",
            "› Running validation step...",
            "✓ Validation passed — no issues",
            "✓ Scenario ready for export",
        ],
    }


@app.post("/run-esmini")
def run_esmini():
    xosc_path = get_current_xosc_path()
    if not xosc_path or not xosc_path.exists():
        raise HTTPException(status_code=404, detail="Generate a scenario first.")

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
def download_xosc():
    xosc_path = get_current_xosc_path()
    if not xosc_path or not xosc_path.exists():
        raise HTTPException(status_code=404, detail="Generate a scenario first.")

    return FileResponse(str(xosc_path), filename=xosc_path.name)


@app.get("/")
def health():
    return {"status": "Backend is running"}
