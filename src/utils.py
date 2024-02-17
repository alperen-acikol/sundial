import ee
import numpy as np
import xarray as xr

from datetime import datetime
from ltgee import LandTrendr

from settings import SQUARE_COLUMNS, MASK_LABELS


def estimate_download_size(
        image: ee.Image,
        geometry: ee.Geometry,
        scale: int) -> tuple[float, float]:
    """
    Estimates the download size of an image based on its pixel count and band dtype.
    This is a rough estimate and may not be accurate for all images since even within the same
    precision, the data size may vary due to compression and other factors.

    Args:
        image (ee.Image): The image to estimate the download size for.
        geometry (ee.Geometry): The geometry to reduce the image over.
        scale (int): The scale to use for the reduction.

    Returns:
        int: The estimated download size in megabytes.
    """
    pixel_count = image.unmask(0).select(0).clip(geometry)\
        .reduceRegion(ee.Reducer.count(), geometry, scale=scale, maxPixels=1e13)\
        .values()\
        .getNumber(0)\
        .getInfo()
    band_count = image.bandNames().size().getInfo()
    data_count = pixel_count * band_count
    match image.bandTypes().values().getInfo()[0]["precision"]:
        case "int16":
            data_count = round((data_count * 2) / 1e6, 2)
        case "int32" | "int":  # int is int32 but due to compression, it may be int 16 in final download
            data_count = round((data_count * 4) / 1e6, 2)
        case "int64" | "double":
            data_count = round((data_count * 8) / 1e6, 2)
    return data_count, pixel_count, band_count


def lt_image_generator(
        start_date: datetime,
        end_date: datetime,
        area_of_interest: ee.Geometry,
        mask_labels: list[str] = MASK_LABELS) -> ee.Image:
    lt = LandTrendr(
        start_date=start_date,
        end_date=end_date,
        area_of_interest=area_of_interest,
        mask_labels=mask_labels,
        run=False
    )
    collection = lt.build_sr_collection()
    size = collection.size().getInfo()

    old_band_names = [f"{str(i)}_{band}" for i in range(size)
                      for band in lt._band_names]
    new_band_names = [f"{str(start_date.year + i)}_{band}" for i in range(size)
                      for band in lt._band_names]
    return lt.build_sr_collection()\
        .toBands()\
        .select(old_band_names, new_band_names)\
        .clipToBoundsAndScale(geometry=area_of_interest, scale=30)


def zarr_reshape(
        arr: np.ndarray,
        polygon_name: str,
        start_year: int,
        end_year: int) -> None:
    xr_list = []
    for year in range(start_year, end_year + 1):
        xr_year = xr.DataArray(np.dstack(
            [arr[f"{year}_B1"],
             arr[f"{year}_B2"],
             arr[f"{year}_B3"],
             arr[f"{year}_B4"],
             arr[f"{year}_B5"],
             arr[f"{year}_B7"]]),
            dims=['x', 'y', "band"])
        xr_list.append(xr_year.astype(float))
    xr_new = xr.concat(xr_list, dim="year")
    xr_new.chunk(chunks={"year": 1})
    xr_new.name = polygon_name
    return xr_new


def generate_name(coords: list[float]) -> str:
    if len(coords) > 1:
        coords = coords[:-1]
    return "_".join([f"x{x}y{y}" for x, y in coords])


def parse_meta_data(
        meta_data: xr.Dataset,
        index: int) -> tuple[list[tuple[float, float]], str, tuple[float, float], str]:
    point_coords = meta_data["point"].isel(
        index=index).values.item()
    point_name = generate_name(point_coords)
    polygon_coords = meta_data[SQUARE_COLUMNS].isel(
        index=index).to_dataarray().values.tolist()
    polygon_name = generate_name(polygon_coords)
    return point_coords, point_name, polygon_coords, polygon_name