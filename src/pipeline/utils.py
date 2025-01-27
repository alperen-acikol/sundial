import ee
import numpy as np
import os
import xarray as xr

from datetime import datetime
from ltgee import LandTrendr

from settings import MASK_LABELS, NO_DATA_VALUE, SQUARE_COLUMNS, STRATA_ATTR_NAME, STRATA_DIM_NAME


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
        square_coords: list[tuple[float, float]],
        scale: int,
        overlap_band: bool,
        overlap_coords: list[tuple[float, float]],
        mask_labels: list[str] = MASK_LABELS) -> ee.Image:
    square = ee.Geometry.Polygon(square_coords)
    lt = LandTrendr(
        start_date=start_date,
        end_date=end_date,
        area_of_interest=square,
        mask_labels=mask_labels,
        run=False
    )
    collection = lt.build_sr_collection()
    size = collection.size().getInfo()

    old_band_names = [f"{str(i)}_{band}" for i in range(size)
                      for band in lt._band_names]
    new_band_names = [f"{str(start_date.year + i)}_{band}" for i in range(size)
                      for band in lt._band_names]

    image = collection\
        .toBands()\
        .select(old_band_names, new_band_names)\

    if overlap_band:
        overlap_area = ee.Geometry.Polygon(overlap_coords)
        overlap_image = ee.Image.constant(1).clip(overlap_area)
        image = image.addBands(overlap_image.select(["constant"], ["overlap"]))
    return image.clipToBoundsAndScale(geometry=square, scale=scale)


def zarr_reshape(
        arr: np.ndarray,
        pixel_edge_size: int,
        square_name: str,
        point_name: str,
        attributes: dict | None,
        strata_map: dict | None) -> xr.DataArray:

    # unflattening the array to shape (year, x, y, band)
    years, bands = zip(*[b.split('_')
                       for b in arr.dtype.names if b != "overlap"])
    years = set(years)
    bands = set(bands)
    xr_list = [
        xr.DataArray(
            np.dstack([arr[f"{y}_{b}"] for b in bands]),
            dims=['x', 'y', "band"]
        ).astype(float)
        for y in years]
    xarr = xr.concat(xr_list, dim="year")

    # adding strata data as attributes
    xarr.name = square_name
    new_attrs = attributes | {"point": point_name}
    xarr.attrs.update(**new_attrs)

    xarr_ann = None
    if "overlap" in arr.dtype.names and STRATA_ATTR_NAME in xarr.attrs.keys():
        overlap = xr.DataArray(arr["overlap"], dims=['x', 'y'])
        stratum = xarr.attrs[STRATA_ATTR_NAME]
        xarr.assign_coords({stratum: overlap})
        stratum_idx = strata_map[stratum]

        # Ideally, we want a probability for each class
        # it is much easier to get a number per class
        # if each strata has it's own tensor
        # TODO: move this to tensor initialization to not store 0 values
        xarr_ann_list = [xr.DataArray(np.zeros(overlap.shape), dims=['x', 'y'], name=stratum)
                         for _ in strata_map]
        xarr_ann = xr.concat(xarr_ann_list, dim=STRATA_DIM_NAME)
        xarr_ann[stratum_idx:stratum_idx+1, :, :] = overlap
        xarr_ann.name = square_name

    # padding the xarray to the edge size to maintain consistent image size in zarr
    if pixel_edge_size > min(xarr["x"].shape[0],  xarr["y"].shape[0]):
        xarr = pad_xy_xarray(xarr, pixel_edge_size)
        if xarr_ann is not None:
            xarr_ann = pad_xy_xarray(xarr_ann, pixel_edge_size)

    xarr = xarr.chunk(chunks={"year": 1})
    if xarr_ann is not None:
        xarr_ann = xarr_ann.chunk(chunks={STRATA_DIM_NAME: 1})

    return xarr, xarr_ann


def pad_xy_xarray(
        xarr: xr.DataArray,
        pixel_edge_size: int) -> xr.DataArray:
    x_diff = pixel_edge_size - xarr["x"].size
    y_diff = pixel_edge_size - xarr["y"].size

    x_start = x_diff // 2
    x_end = x_diff - x_start

    y_start = y_diff // 2
    y_end = y_diff - y_start

    xarr = xarr.pad(
        x=(x_start, x_end),
        y=(y_start, y_end),
        keep_attrs=True,
        mode="constant",
        constant_values=NO_DATA_VALUE)
    return xarr


def generate_coords_name(coords: tuple[float]) -> str:
    if len(coords) > 2:
        coords = coords[:-1]
        return "_".join([f"x{x}y{y}" for x, y in coords])
    else:
        return f"x{coords[0]}y{coords[1]}"


def parse_meta_data(
        meta_data: xr.Dataset,
        index: int,
        back_step: int) -> tuple[list[tuple[float, float]],
                                 str,
                                 tuple[float, float],
                                 str,
                                 datetime | None,
                                 datetime | None,
                                 dict]:
    # this is a bit of a hack to get around the fact that return multiple types in one go is a bit of a pain
    geometry_columns = [k for k in meta_data.keys() if "geometry_" in k]
    geometry_values = meta_data[geometry_columns].isel(
        index=index).to_dataarray().values.tolist()
    geometry = [p for p in geometry_values if all(c == c for c in p)]

    # pulling directly to preserve order for polygon unamibguity
    point_coords = meta_data["point_coords"].isel(
        index=index).values.item()
    point_name = meta_data["point_name"].isel(
        index=index).values.item()
    square_coords = meta_data[SQUARE_COLUMNS].isel(
        index=index).to_dataarray().values.tolist()
    square_name = meta_data["square_name"].isel(
        index=index).values.item()

    # generating start and end date from year attribute and back step
    if "year" in meta_data.variables.keys():
        end_year = meta_data["year"].isel(
            index=index).values.item()
        end_date = datetime(end_year, 9, 1)
        start_date = datetime(end_year - back_step, 6, 1)
    else:
        start_date = None
        end_date = None

    data_vars = meta_data.isel(index=index).to_dict()["data_vars"].items()
    attributes = {k: v["data"] for k, v in data_vars
                  if all([s not in k for s in ["geometry", "square", "point"]])}
    return geometry, \
        point_coords, \
        point_name, \
        square_coords, \
        square_name, \
        start_date, \
        end_date, \
        attributes
