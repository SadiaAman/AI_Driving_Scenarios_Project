"""
Phase 5 (start): parallel scenario-building helpers using the `scenariogeneration`
library.

These helpers are intentionally NOT wired into the FastAPI pipeline yet — main.py
still generates the full XOSC via manual XML strings, and that path keeps working
untouched. This module exists so we can migrate one block at a time and verify, at
each step, that scenariogeneration reproduces equivalent OpenSCENARIO output before
swapping anything in.

First migrated block: the Environment (time of day + weather + road condition),
which is self-contained and easy to validate in isolation.
"""

from datetime import datetime
from pathlib import Path
from xml.etree import ElementTree as ET

from scenariogeneration import xosc, xodr


def build_lane_road_xodr(lane_count: int, out_dir) -> Path:
    """
    Phase 4: generate (and cache) a straight OpenDRIVE road with `lane_count`
    driving lanes total — lane_count/2 in each direction. esmini renders the
    road from this .xodr alone (no separate 3D model needed). Returns the path.
    """
    per_side = max(1, lane_count // 2)
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"road_{lane_count}lane.xodr"
    if path.exists():
        return path  # cached

    road = xodr.create_road(
        xodr.Line(200), id=0, left_lanes=per_side, right_lanes=per_side
    )
    odr = xodr.OpenDrive(f"road_{lane_count}lane")
    odr.add_road(road)
    odr.adjust_roads_and_lanes()
    odr.write_xml(str(path))
    return path


def build_environment(
    date_time: str,
    cloud_cover: str,
    precip_type: str,
    precip_intensity: str,
    fog_range: str,
    friction: str,
    sun_intensity: str,
) -> xosc.Environment:
    """
    Build a scenariogeneration Environment equivalent to the one main.py emits
    manually. Inputs are the same values produced by main.get_environment(), so
    the two builders can be compared directly.
    """
    dt = datetime.strptime(date_time, "%Y-%m-%dT%H:%M:%S")
    time_of_day = xosc.TimeOfDay(
        False, dt.year, dt.month, dt.day, dt.hour, dt.minute, dt.second
    )

    weather = xosc.Weather(
        cloudstate=getattr(xosc.FractionalCloudCover, cloud_cover),
        sun=xosc.Sun(float(sun_intensity), 0.0, 1.31),
        fog=xosc.Fog(int(float(fog_range))),
        precipitation=xosc.Precipitation(
            getattr(xosc.PrecipitationType, precip_type), float(precip_intensity)
        ),
    )

    road_condition = xosc.RoadCondition(float(friction))

    return xosc.Environment(
        "GeneratedEnvironment", time_of_day, weather, road_condition
    )


def environment_action_xml(*args, **kwargs) -> str:
    """Return the EnvironmentAction as a pretty-printed XML string."""
    action = xosc.EnvironmentAction(build_environment(*args, **kwargs))
    element = action.get_element()
    ET.indent(element, space="   ")
    return ET.tostring(element, encoding="unicode")


def build_entities_xml(
    npc_catalog: str | None,
    pedestrian_model: str,
    traffic_models: list,
    num_traffic_vehicles: int,
) -> str:
    """
    Second migrated block: the <Entities> list, built with scenariogeneration.

    - Ego: a VehicleCatalog reference to the $HostVehicle parameter.
    - NPC: a VehicleCatalog reference when npc_catalog is given (car / truck /
      cyclist), otherwise an inline Pedestrian (walkman) with scaleMode set, to
      match what esmini expects for pedestrian model scaling.
    - TrafficVehicle_n: VehicleCatalog references cycling through traffic_models.
    """
    entities = xosc.Entities()
    entities.add_scenario_object(
        "Ego", xosc.CatalogReference("VehicleCatalog", "$HostVehicle")
    )

    if npc_catalog:
        entities.add_scenario_object(
            "NPC", xosc.CatalogReference("VehicleCatalog", npc_catalog)
        )
    else:
        bounding_box = xosc.BoundingBox(0.5, 0.6, 1.8, 0.06, 0.0, 0.923)
        pedestrian = xosc.Pedestrian(
            "NPC", 80, xosc.PedestrianCategory.pedestrian, bounding_box,
            model=pedestrian_model,
        )
        pedestrian.add_property("scaleMode", "BBToModel")
        entities.add_scenario_object("NPC", pedestrian)

    for index in range(num_traffic_vehicles):
        model = traffic_models[index % len(traffic_models)]
        entities.add_scenario_object(
            f"TrafficVehicle_{index + 1}",
            xosc.CatalogReference("VehicleCatalog", model),
        )

    element = entities.get_element()
    ET.indent(element, space="   ")
    return ET.tostring(element, encoding="unicode")


def build_ego_init_xml(host_speed_ms: float) -> str:
    """
    Third migrated block (Init slice, Ego only): the Ego's Init private actions —
    assign the fabriksgatan route, teleport onto it (RoutePosition gives the
    correct heading), and set the start speed. Returns the <Private entityRef="Ego">
    element as a string. NPC and traffic init stay manual for now.
    """
    route = xosc.CatalogReference("RoutesAtFabriksgatan", "HostStraightRoute")
    init = xosc.Init()
    init.add_init_action(
        "Ego", xosc.AssignRouteAction(
            xosc.CatalogReference("RoutesAtFabriksgatan", "HostStraightRoute")
        )
    )
    init.add_init_action(
        "Ego", xosc.TeleportAction(
            xosc.RoutePositionInLaneCoordinates(route, s=0, laneid=1, offset=0.0)
        )
    )
    init.add_init_action(
        "Ego", xosc.AbsoluteSpeedAction(
            host_speed_ms,
            xosc.TransitionDynamics(
                xosc.DynamicsShapes.step, xosc.DynamicsDimension.time, 0.0
            ),
        )
    )
    private = init.get_element().find("Actions/Private")
    ET.indent(private, space="   ")
    return ET.tostring(private, encoding="unicode")


if __name__ == "__main__":
    # Demo: print the EnvironmentAction for a clear day, to eyeball against the
    # manual XML in main.build_xosc_preview().
    print(
        environment_action_xml(
            "2026-06-07T12:00:00", "zeroOktas", "dry", "0.0", "100000", "1.0", "100000"
        )
    )
