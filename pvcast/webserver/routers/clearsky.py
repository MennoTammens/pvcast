"""This module contains the FastAPI router for the /clearsky endpoint."""
from __future__ import annotations

import logging

import pandas as pd
from fastapi import APIRouter, Depends
from typing_extensions import Annotated

from ...model.forecasting import ForecastResult
from ...model.model import PVSystemManager
from ...weather.weather import WeatherAPI
from ..models.base import Interval, PVPlantNames, StartEndRequest
from ..models.clearsky import ClearskyModel
from ..routers.dependencies import get_pv_system_mngr, get_weather_api
from .helpers import multi_idx_to_nested_dict

router = APIRouter()

_LOGGER = logging.getLogger("uvicorn")


@router.post("/{plant_name}/{interval}")
def post(
    plant_name: PVPlantNames,
    pv_system_mngr: Annotated[PVSystemManager, Depends(get_pv_system_mngr)],
    weather_api: Annotated[WeatherAPI, Depends(get_weather_api)],
    start_end: StartEndRequest = None,
    interval: Interval = Interval.H1,
) -> ClearskyModel:
    """Get the estimated PV output power in Watts and energy in Wh at the given interval <interval> \
    for the given PV system <name>.

    POST: This will force a recalculation of the power output using the latest available weather data,\
    which may take some time.

    If no request body is provided, the first timestamp will be the current time and the last timestamp will be\
    the current time + interval.

    NB: Energy data is configured to represent the state at the beginning of the interval and what is going to happen \
    in this interval.

    :param plant_name: Name of the PV system
    :param interval: Interval of the returned data
    :return: Estimated PV power output in Watts at the given interval <interval> for the given PV system <name>
    """
    location = pv_system_mngr.location

    # build the datetime index
    if start_end is None:
        _LOGGER.info("No start and end timestamps provided, using current time and interval")
        datetimes = weather_api.get_source_dates(weather_api.start_forecast, weather_api.end_forecast, interval)
    else:
        datetimes = weather_api.get_source_dates(start_end.start, start_end.end, interval)

    # loop over all PV plants and find the one with the given name
    all_arg = plant_name.name.lower() == "all"
    pv_plant_names = list(pv_system_mngr.pv_plants.keys()) if all_arg else [plant_name.name]

    # build multi-index columns
    cols = [("watt", pv_plant) for pv_plant in pv_plant_names]
    cols += [("watt_hours", pv_plant) for pv_plant in pv_plant_names]
    cols += [("watt_hours_cumsum", pv_plant) for pv_plant in pv_plant_names]
    cols += [("watt_hours", "Total")] if all_arg else []
    cols += [("watt_hours_cumsum", "Total")] if all_arg else []
    cols += [("watt", "Total")] if all_arg else []
    multi_index = pd.MultiIndex.from_tuples(cols, names=["type", "plant"])

    # build the result dataframe
    result_df = pd.DataFrame(columns=multi_index)

    # loop over all PV plants and compute the clearsky power output
    for pv_plant in pv_plant_names:
        _LOGGER.info("Estimating clearsky performance for plant: %s", pv_plant)

        # compute the clearsky power output for the given PV system and datetimes
        try:
            pvplant = pv_system_mngr.get_pv_plant(pv_plant)
        except KeyError:
            _LOGGER.error("No PV system found with plant_name %s", plant_name)
            continue

        # run forecasting algorithm
        clearsky_output: ForecastResult = pvplant.clearsky.run(weather_df=datetimes)

        # convert ac power timestamps to string
        ac_power: pd.Series = clearsky_output.ac_power
        ac_energy: pd.Series = clearsky_output.ac_energy
        ac_power.index = ac_power.index.strftime("%Y-%m-%dT%H:%M:%S%z")
        ac_energy.index = ac_energy.index.strftime("%Y-%m-%dT%H:%M:%S%z")

        # build the output dataframe with multi-index
        result_df[("watt", pv_plant)] = ac_power
        result_df[("watt_hours", pv_plant)] = ac_energy
        result_df[("watt_hours_cumsum", pv_plant)] = ac_energy.cumsum()

    # if all_arg, sum the power and energy columns
    if all_arg:
        result_df[("watt", "Total")] = result_df["watt"].sum(axis=1)
        result_df[("watt_hours", "Total")] = result_df["watt_hours"].sum(axis=1)
        result_df[("watt_hours_cumsum", "Total")] = result_df["watt_hours_cumsum"].sum(axis=1)

    # check if there are any NaN values in the result
    if result_df.isnull().values.any():
        raise ValueError(f"NaN values in the result dataframe: \n{result_df}")

    # round all columns and set all values to int64
    result_df = result_df.round(0).astype(int)

    # convert multi index to nested dict
    result_df = dict(multi_idx_to_nested_dict(result_df.T))

    # build the response dict
    response_dict = {
        "interval": interval,
        "start": ac_power.index[0],
        "end": ac_power.index[-1],
        "timezone": location.tz,
        "result": result_df,
    }

    return ClearskyModel(**response_dict)
