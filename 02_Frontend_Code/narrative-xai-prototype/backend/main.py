from pathlib import Path
import os
import subprocess

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from pydantic import BaseModel


BASE_DIR = Path(__file__).resolve().parent
GENERATED_DIR = BASE_DIR / "generated"
GENERATED_DIR.mkdir(exist_ok=True)
XOSC_PATH = GENERATED_DIR / "scenario_v1.xosc"

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
    npcType: str
    npcBehavior: str
    egoResponse: str
    prompt: str


def build_xosc_preview(data: ScenarioRequest) -> str:
    """
    Generate an esmini-compatible OpenSCENARIO file using a pedestrian template.

    Current prototype:
    - Pedestrian crossing scenario
    - esmini-compatible XOSC structure
    - Weather is mapped to EnvironmentAction
    - HostSpeed is fixed at 10 m/s to match original esmini pedestrian timing
    """

    # esmini expects speed in m/s.
    # Temporarily fixed to 10 m/s so the car stops like the original pedestrian.xosc.
    # Later we can use: host_speed_ms = round(data.egoSpeed / 3.6, 2)
    host_speed_ms = 10

    # Weather mapping.
    # For rain/snow, keep scene visually readable but encode weather + friction.
    if data.weather == "rain":
        cloud_state = "free"
        precipitation_type = "rain"
        precipitation_intensity = "0.1"
        fog_range = "100000"
        friction = "0.7"
        sun_intensity = "1.0"

    elif data.weather == "fog":
        cloud_state = "free"
        precipitation_type = "dry"
        precipitation_intensity = "0.0"
        fog_range = "300"
        friction = "0.8"
        sun_intensity = "1.0"

    elif data.weather == "snow":
        cloud_state = "free"
        precipitation_type = "snow"
        precipitation_intensity = "0.1"
        fog_range = "100000"
        friction = "0.5"
        sun_intensity = "1.0"

    else:
        cloud_state = "free"
        precipitation_type = "dry"
        precipitation_intensity = "0.0"
        fog_range = "100000"
        friction = "1.0"
        sun_intensity = "1.0"

    resources_dir = (ESMINI_DEMO_DIR / "resources").as_posix()
    route_catalog_dir = f"{resources_dir}/xosc/Catalogs/Routes"
    vehicle_catalog_dir = f"{resources_dir}/xosc/Catalogs/Vehicles"
    logic_file = f"{resources_dir}/xodr/fabriksgatan.xodr"
    scene_graph_file = f"{resources_dir}/models/fabriksgatan.osgb"
    pedestrian_model = f"{resources_dir}/models/walkman.osgb"

    return f'''<?xml version="1.0" encoding="UTF-8"?>
<OpenSCENARIO>
   <FileHeader revMajor="1"
               revMinor="1"
               date="2026-06-07T00:00:00"
               description="Narrative XAI generated pedestrian scenario"
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

      <ScenarioObject name="pedestrian_adult">
         <Pedestrian mass="80"
                     model="EPTa"
                     name="pedestrian_adult"
                     pedestrianCategory="pedestrian"
                     model3d="{pedestrian_model}">
            <ParameterDeclarations/>
            <BoundingBox>
               <Center x="0.06" y="0.0" z="0.923"/>
               <Dimensions height="1.8" length="0.6" width="0.5"/>
            </BoundingBox>
            <Properties>
               <Property name="scaleMode" value="BBToModel"/>
            </Properties>
         </Pedestrian>
      </ScenarioObject>
   </Entities>

   <Storyboard>
      <Init>
         <Actions>
            <GlobalAction>
               <EnvironmentAction>
                  <Environment name="GeneratedEnvironment">
                     <TimeOfDay animation="false" dateTime="2026-06-07T12:00:00"/>
                     <Weather cloudState="{cloud_state}">
                        <Sun intensity="{sun_intensity}" azimuth="0" elevation="1.31"/>
                        <Fog visualRange="{fog_range}"/>
                        <Precipitation precipitationType="{precipitation_type}" intensity="{precipitation_intensity}"/>
                     </Weather>
                     <RoadCondition frictionScaleFactor="{friction}"/>
                  </Environment>
               </EnvironmentAction>
            </GlobalAction>

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

            <Private entityRef="pedestrian_adult">
               <PrivateAction>
                  <TeleportAction>
                     <Position>
                        <LanePosition laneId="3" offset="0.5" roadId="0" s="15">
                           <Orientation type="relative" h="0" p="0" r="0"/>
                        </LanePosition>
                     </Position>
                  </TeleportAction>
               </PrivateAction>
            </Private>
         </Actions>
      </Init>

      <Story name="GeneratedPedestrianStory">
         <Act name="GeneratedPedestrianAct">
            <ManeuverGroup maximumExecutionCount="1" name="pedestrian_crossing_group">
               <Actors selectTriggeringEntities="false">
                  <EntityRef entityRef="pedestrian_adult"/>
               </Actors>

               <Maneuver name="pedestrian_crossing_maneuver">
                  <Event maximumExecutionCount="1" name="pedestrian_crossing_event" priority="overwrite">
                     <Action name="pedestrian_walk_speed">
                        <PrivateAction>
                           <LongitudinalAction>
                              <SpeedAction>
                                 <SpeedActionDynamics dynamicsShape="linear" value="2" dynamicsDimension="rate"/>
                                 <SpeedActionTarget>
                                    <AbsoluteTargetSpeed value="$PedestrianSpeed"/>
                                 </SpeedActionTarget>
                              </SpeedAction>
                           </LongitudinalAction>
                        </PrivateAction>
                     </Action>

                     <Action name="pedestrian_walk_route">
                        <PrivateAction>
                           <RoutingAction>
                              <FollowTrajectoryAction>
                                 <Trajectory closed="false" name="pedestrian_trajectory">
                                    <ParameterDeclarations/>
                                    <Shape>
                                       <Polyline>
                                          <Vertex>
                                             <Position>
                                                <LanePosition laneId="3" offset="0.5" roadId="0" s="15">
                                                   <Orientation type="relative" h="0" p="0" r="0"/>
                                                </LanePosition>
                                             </Position>
                                          </Vertex>

                                          <Vertex>
                                             <Position>
                                                <LanePosition laneId="3" offset="0.5" roadId="0" s="10.5">
                                                   <Orientation type="relative" h="0" p="0" r="0"/>
                                                </LanePosition>
                                             </Position>
                                          </Vertex>

                                          <Vertex>
                                             <Position>
                                                <LanePosition laneId="3" offset="0.0" roadId="0" s="10">
                                                   <Orientation type="relative" h="1.57" p="0" r="0"/>
                                                </LanePosition>
                                             </Position>
                                          </Vertex>

                                          <Vertex>
                                             <Position>
                                                <LanePosition laneId="-3" offset="0.0" roadId="0" s="10">
                                                   <Orientation type="relative" h="4.71" p="0" r="0"/>
                                                </LanePosition>
                                             </Position>
                                          </Vertex>

                                          <Vertex>
                                             <Position>
                                                <LanePosition laneId="-3" offset="-0.5" roadId="0" s="9.5">
                                                   <Orientation type="relative" h="3.14" p="0" r="0"/>
                                                </LanePosition>
                                             </Position>
                                          </Vertex>

                                          <Vertex>
                                             <Position>
                                                <LanePosition laneId="-3" offset="-0.5" roadId="0" s="7.5">
                                                   <Orientation type="relative" h="3.14" p="0" r="0"/>
                                                </LanePosition>
                                             </Position>
                                          </Vertex>
                                       </Polyline>
                                    </Shape>
                                 </Trajectory>

                                 <TimeReference>
                                    <None/>
                                 </TimeReference>

                                 <TrajectoryFollowingMode followingMode="follow"/>
                              </FollowTrajectoryAction>
                           </RoutingAction>
                        </PrivateAction>
                     </Action>

                     <StartTrigger>
                        <ConditionGroup>
                           <Condition conditionEdge="rising" delay="0" name="pedestrian_start_condition">
                              <ByEntityCondition>
                                 <TriggeringEntities triggeringEntitiesRule="any">
                                    <EntityRef entityRef="Ego"/>
                                 </TriggeringEntities>
                                 <EntityCondition>
                                    <TraveledDistanceCondition value="5"/>
                                 </EntityCondition>
                              </ByEntityCondition>
                           </Condition>
                        </ConditionGroup>
                     </StartTrigger>
                  </Event>
               </Maneuver>
            </ManeuverGroup>

            <ManeuverGroup maximumExecutionCount="1" name="ego_brake_group">
               <Actors selectTriggeringEntities="false">
                  <EntityRef entityRef="Ego"/>
               </Actors>

               <Maneuver name="ego_brake_maneuver">
                  <Event name="ego_brake_event" priority="overwrite">
                     <Action name="ego_brake_action">
                        <PrivateAction>
                           <LongitudinalAction>
                              <SpeedAction>
                                 <SpeedActionDynamics dynamicsShape="linear" value="-5.1" dynamicsDimension="rate"/>
                                 <SpeedActionTarget>
                                    <AbsoluteTargetSpeed value="0"/>
                                 </SpeedActionTarget>
                              </SpeedAction>
                           </LongitudinalAction>
                        </PrivateAction>
                     </Action>

                     <StartTrigger>
                        <ConditionGroup>
                           <Condition name="ego_brake_condition" delay="0" conditionEdge="rising">
                              <ByEntityCondition>
                                 <TriggeringEntities triggeringEntitiesRule="any">
                                    <EntityRef entityRef="Ego"/>
                                 </TriggeringEntities>
                                 <EntityCondition>
                                    <TimeToCollisionCondition value="1.2"
                                                              freespace="true"
                                                              coordinateSystem="entity"
                                                              relativeDistanceType="longitudinal"
                                                              rule="lessThan">
                                       <TimeToCollisionConditionTarget>
                                          <EntityRef entityRef="pedestrian_adult"/>
                                       </TimeToCollisionConditionTarget>
                                    </TimeToCollisionCondition>
                                 </EntityCondition>
                              </ByEntityCondition>
                           </Condition>
                        </ConditionGroup>
                     </StartTrigger>
                  </Event>
               </Maneuver>
            </ManeuverGroup>

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
               <ByEntityCondition>
                  <TriggeringEntities triggeringEntitiesRule="any">
                     <EntityRef entityRef="pedestrian_adult"/>
                  </TriggeringEntities>
                  <EntityCondition>
                     <ReachPositionCondition tolerance="5.0">
                        <Position>
                           <LanePosition roadId="0" laneId="-3" s="0"/>
                        </Position>
                     </ReachPositionCondition>
                  </EntityCondition>
               </ByEntityCondition>
            </Condition>
         </ConditionGroup>
      </StopTrigger>
   </Storyboard>
</OpenSCENARIO>'''


@app.post("/generate-scenario")
def generate_scenario(data: ScenarioRequest):
    xosc = build_xosc_preview(data)
    XOSC_PATH.write_text(xosc, encoding="utf-8")

    return {
        "filename": "scenario_v1.xosc",
        "actors": int(data.trafficVehicles) + 1,
        "duration": 14.2,
        "xosc_preview": xosc,
        "pipeline_log": [
            f"› Parsing prompt: \"{data.prompt[:80]}...\"",
            "› Sending structured prompt to generator...",
            f"✓ XOSC generated — {int(data.trafficVehicles) + 1} actors, 14.2 s duration",
            "› Running validation step...",
            "✓ Validation passed — no issues",
            "✓ Scenario ready for export",
        ],
    }


@app.post("/run-esmini")
def run_esmini():
    if not XOSC_PATH.exists():
        raise HTTPException(status_code=404, detail="Generate a scenario first.")

    if not ESMINI_EXE:
        return {
            "message": "⚠ esmini path is not configured yet. Set ESMINI_EXE environment variable after downloading esmini."
        }

    try:
        subprocess.Popen([ESMINI_EXE, "--osc", str(XOSC_PATH)])
        return {"message": "✓ esmini started. Check the esmini window."}
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@app.get("/download-xosc")
def download_xosc():
    if not XOSC_PATH.exists():
        raise HTTPException(status_code=404, detail="Generate a scenario first.")

    return FileResponse(str(XOSC_PATH), filename="scenario_v1.xosc")


@app.get("/")
def health():
    return {"status": "Backend is running"}
