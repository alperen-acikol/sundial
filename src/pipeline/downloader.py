import argparse
import ee
import numpy as np
import multiprocessing as mp
import os
import time
import utm
import xarray as xr
import yaml
import zarr

from datetime import datetime
from pathlib import Path
from typing import Literal
from zarr.errors import PathNotFoundError, GroupNotFoundError, ArrayNotFoundError

from utils import parse_meta_data, estimate_download_size, lt_image_generator, zarr_reshape
from logger import get_logger
from settings import FILE_EXT_MAP, GEE_REQUEST_LIMIT


EE_END_POINT = 'https://earthengine-highvolume.googleapis.com'


class Downloader:
    """
    A class for downloading images from Google Earth Engine via squares and date filters.

    Args:
        start_date (datetime): The start date to filter image collection.
        end_date (datetime): The end date to filter image collection.
        file_type (Literal["GEO_TIFF", "ZARR", "NPY", "NUMPY_NDARRAY"]): The file type to save the image data as.
        overwrite (bool): A flag to overwrite existing image data.
        scale (int): The scale to use for projecting image.
        pixel_edge_size (int): The edge size to use to calculate padding.
        reprojection (str): A str flag to reproject the image data if set.
        overlap_band (bool): A flag to add a band that labels by pixel in the square whether the it overlaps the geometry.
        back_step (int): The number of years to step back from the end date.

        chip_data_path (str): The path to save the image data to.
        anno_data_path (str): The path to the strata map file.
        meta_data_path (str): The path to the meta data file with coordinates.

        num_workers (int): The number of workers to use for the parallel download process.
        retries (int): The number of retries to use for the download process.
        request_limit (int): The number of requests to make at a time.
        ignore_size_limit (bool): A flag to ignore the size limits for the image data.
        io_lock (bool): A flag to use a lock for the io process.

        log_path (str): The path to save the log file to.
        log_name (str): The name of the log file.

    Methods:
        start(): Starts the parallel download process and performs the necessary checks.
    """
    _size_limit: int = 65
    _band_limit: int = 1024
    _pixel_limit: int = 3.2e4

    def __init__(
            self,
            start_date: datetime | None,
            end_date: datetime | None,
            file_type: Literal["GEO_TIFF", "ZARR", "NPY", "NUMPY_NDARRAY"],
            overwrite: bool,
            scale: int,
            pixel_edge_size: int,
            reprojection: bool,
            overlap_band: bool,
            back_step: int,
            chip_data_path: str,
            strata_map_path: str,
            anno_data_path: str,
            meta_data_path: str,
            num_workers: int,
            retries: int,
            ignore_size_limit: bool,
            io_lock: bool,
            log_path: str,
            log_name: str,
    ) -> None:
        self._start_date = start_date
        self._end_date = end_date
        self._file_type = file_type
        self._overwrite = overwrite
        self._scale = scale
        self._pixel_edge_size = pixel_edge_size
        self._reprojection = reprojection
        self._overlap_band = overlap_band
        self._back_step = back_step
        self._chip_data_path = chip_data_path
        self._strata_map_path = strata_map_path
        self._anno_data_path = anno_data_path
        self._meta_data_path = meta_data_path
        self._num_workers = num_workers
        self._retries = retries
        self._ignore_size_limit = ignore_size_limit
        self._io_lock = io_lock
        self._log_path = log_path
        self._log_name = log_name

        # TODO: Parameterize the image generator callable
        # TODO: Perform attribute checks
        self._image_gen_callable = lt_image_generator
        self._meta_data = xr.open_zarr(self._meta_data_path)
        self._meta_size = self._meta_data["index"].size

    def start(self) -> None:
        """
        Starts the parallel download process and performs the necessary checks.
        """
        if not self._ignore_size_limit:
            # this assumes all squares are the same size
            _, _, _, square_coords, start_date, end_date, _ = parse_meta_data(
                self._meta_data, 0, self._back_step)
            test_area = ee.Geometry.Polygon(square_coords)
            if start_date is None:
                start_date = self._start_date
            if end_date is None:
                end_date = self._end_date

            test_image = self._image_gen_callable(
                start_date, end_date, test_area, self._scale)
            test_size, test_pixels, test_bands = estimate_download_size(
                test_image, test_area, self._scale)
            if test_size > self._size_limit:
                raise ValueError(
                    f"Image size of {test_size}MB exceeds size limit of {self._size_limit}MB. Please reduce the size of the image.")
            if test_pixels**.5 > self._pixel_limit:
                raise ValueError(
                    f"Pixel count of {test_pixels} exceeds pixel limit of {self._pixel_limit}. Please reduce the pixels of the image.")
            if test_bands > self._band_limit:
                raise ValueError(
                    f"Band count of {test_bands} exceeds band limit of {self._band_limit}. Please reduce the bands of the image.")
        self._watcher()

    def _watcher(self) -> None:
        # intialize the multiprocessing manager and queues
        manager = mp.Manager()
        image_queue = manager.Queue()
        result_queue = manager.Queue()
        report_queue = manager.Queue()
        chip_io_lock = manager.Lock()
        ann_io_lock = manager.Lock()
        request_limiter = manager.Semaphore(GEE_REQUEST_LIMIT)
        workers = set()

        # create reporter, image generator, and consumer processes
        reporter = mp.Process(
            target=self._reporter,
            args=[report_queue],
            daemon=True)
        workers.add(reporter)

        image_generator = mp.Process(
            target=self._image_generator,
            args=(
                image_queue,
                result_queue,
                report_queue),
            daemon=True)
        workers.add(image_generator)

        for _ in range(self._num_workers):
            image_consumer = mp.Process(
                target=self._image_consumer,
                args=(
                    image_queue,
                    result_queue,
                    report_queue,
                    chip_io_lock,
                    ann_io_lock,
                    request_limiter),
                daemon=True)
            workers.add(image_consumer)

        # start download and watch for results
        start_time = time.time()
        [w.start() for w in workers]
        report_queue.put(("INFO",
                         f"Starting download of {self._meta_size} points of interest..."))
        idx = 0
        jdx = 0
        while idx < self._meta_size:
            # TODO: perform result checks and monitor gee processes
            result = result_queue.get()
            if result is not None:
                idx += 1
                report_queue.put(
                    ("INFO", f"{idx}/{self._meta_size} Completed. {result}"))
            else:
                jdx += 1
                report_queue.put(
                    ("INFO", f"{jdx}/{self._num_workers} Workers terminated."))
        end_time = time.time()
        report_queue.put(("INFO",
                         f"Download completed in {(end_time - start_time) / 60:.2} minutes."))
        report_queue.put(("INFO", "Finalizing last file writes."))
        report_queue.put(None)
        [w.join() for w in workers]

    def _reporter(self, report_queue: mp.Queue) -> None:
        logger = get_logger(self._log_path, self._log_name)
        while (report := report_queue.get()) is not None:
            level, message = report
            match level:
                case "DEBUG":
                    logger.debug(message)
                case "INFO":
                    logger.info(message)
                case "WARNING":
                    logger.warning(message)
                case "ERROR":
                    logger.error(message)
                case "CRITICAL":
                    logger.critical(message)
                case "EXIT":
                    return

    def _image_generator(self,
                         image_queue: mp.Queue,
                         result_queue: mp.Queue,
                         report_queue: mp.Queue
                         ) -> None:
        ee.Initialize(opt_url=EE_END_POINT)
        file_ext = FILE_EXT_MAP[self._file_type]
        for idx in range(self._meta_size):
            try:
                # reading meta data from xarray
                geometry_coords, \
                    point_coords, \
                    point_name, \
                    square_coords, \
                    square_name, \
                    start_date, \
                    end_date, \
                    attributes \
                    = parse_meta_data(self._meta_data, idx, self._back_step)
                if start_date is None:
                    start_date = self._start_date
                if end_date is None:
                    end_date = self._end_date

                # checking for existing files and skipping if file found
                if self._file_type != "ZARR":
                    chip_data_path = os.path.join(self._chip_data_path,
                                                  f"{square_name}.{file_ext}")
                    anno_data_path = os.path.join(self._anno_data_path,
                                                  f"{square_name}.{file_ext}")
                    if not self._overwrite and Path(chip_data_path).exists() and Path(anno_data_path).exists():
                        report_queue.put(
                            "INFO", f"File {chip_data_path} already exists. Skipping...")
                        result_queue.put(square_name)
                        continue
                else:
                    chip_data_path = self._chip_data_path
                    anno_data_path = self._anno_data_path
                    if not self._overwrite:
                        try:
                            # opening with read only mode to check for existing zarr groups
                            zarr.open(
                                store=chip_data_path,
                                mode="r")[square_name]
                            zarr.open(
                                store=anno_data_path,
                                mode="r")[square_name]
                            report_queue.put(("INFO",
                                              f"Polygon already exists at path. Skipping... {square_name}"))
                            continue

                        except (PathNotFoundError,
                                GroupNotFoundError,
                                ArrayNotFoundError,
                                KeyError,
                                FileNotFoundError) as e:
                            # capturing valid exceptions and passing to next step
                            report_queue.put(
                                ("INFO", f"Valid exception captured for square: {type(e)}... {square_name}"))
                            pass

                        except Exception as e:
                            # capturing fatal exceptions and skipping to next square
                            report_queue.put(
                                ("CRITICAL", f"Failed to read zarr path {chip_data_path}, zarr group {point_name}, or zarr variable {square_name} skipping: {type(e)} {e}"))
                            result_queue.put(square_name)
                            continue

                # creating payload for each square to send to GEE
                report_queue.put(
                    ("INFO", f"Creating image payload for square... {square_name}"))
                report_queue.put(
                    ("INFO", f"{geometry_coords}... {square_coords}"))
                image = self._image_gen_callable(
                    start_date,
                    end_date,
                    square_coords,
                    self._scale,
                    self._overlap_band,
                    geometry_coords)

                # Reprojecting the image if necessary
                match self._reprojection:
                    case "UTM":
                        revserse_point = reversed(point_coords)
                        utm_zone = utm.from_latlon(*revserse_point)[-2:]
                        epsg_prefix = "EPSG:326" if point_coords[1] > 0 else "EPSG:327"
                        epsg_code = f"{epsg_prefix}{utm_zone[0]}"
                    case _:
                        epsg_code = self._reprojection
                if epsg_code is not None:
                    report_queue.put(
                        ("INFO", f"Reprojecting image payload square to {epsg_code}... {square_name}"))
                    image = image.reproject(
                        crs=epsg_code, scale=self._scale)

                # encoding the image for the image consumer
                payload = {
                    "expression": ee.serializer.encode(image),
                    "fileFormat": self._file_type if self._file_type != "ZARR" else "NUMPY_NDARRAY",
                }

                # sending payload to the image consumer
                image_queue.put(
                    (payload, square_name, point_name, chip_data_path, anno_data_path, attributes))
            except Exception as e:
                report_queue.put(
                    ("CRITICAL", f"Failed to create image payload for square skipping: {type(e)} {e} {square_name}"))
                result_queue.put(square_name)
        [image_queue.put(None) for i in range(self._num_workers)]

    def _image_consumer(self,
                        image_queue: mp.Queue,
                        result_queue: mp.Queue,
                        report_queue: mp.Queue,
                        chip_io_lock: mp.Lock,
                        ann_io_lock: mp.Lock,
                        request_limiter: mp.Semaphore) -> None:
        ee.Initialize(opt_url=EE_END_POINT)
        with open(self._strata_map_path, "r") as f:
            strata_map = yaml.safe_load(f)

        while (image_task := image_queue.get()) is not None:
            payload, square_name, point_name, chip_data_path, anno_data_path, attributes = image_task
            attempts = 0
            arr = None

            # attempt to download the image from gee
            while attempts < self._retries:
                attempts += 1
                try:
                    report_queue.put(("INFO",
                                     f"Requesting Image pixels for square... {square_name}"))
                    with request_limiter:
                        # TODO: implement retry.Retry decorator
                        payload["expression"] = ee.deserializer.decode(
                            payload["expression"])
                        arr = ee.data.computePixels(payload)
                        break
                except Exception as e:
                    time.sleep(3)
                    report_queue.put(
                        ("WARNING", f"Failed to download square attempt {attempts}/{self._retries}: {type(e)} {e} {square_name}"))
                    if attempts == self._retries:
                        report_queue.put(
                            ("ERROR", f"Max retries reached for square skipping... {square_name}"))
                        result_queue.put(square_name)
                    else:
                        report_queue.put(
                            ("INFO", f"Retrying download for square... {square_name}"))

            # write the image to disk if successful
            if arr is not None:
                report_queue.put(
                    ("INFO", f"Attempting to save square to {chip_data_path}... {square_name}"))
                try:
                    # TODO: perform reshaping along years for non zarr file types
                    match self._file_type:
                        case "NPY" | "GEO_TIFF":
                            out_file = Path(chip_data_path)
                            out_file.write_bytes(arr)
                        case "NUMPY_NDARRAY":
                            np.save(chip_data_path, arr)
                        case "ZARR":
                            report_queue.put((
                                "INFO", f"Reshaping square {arr.shape} to zarr... {square_name}"))
                            xarr_chip, xarr_anno = zarr_reshape(arr,
                                                                self._pixel_edge_size,
                                                                square_name,
                                                                point_name,
                                                                attributes,
                                                                strata_map)

                            # unfortunately, xarray .zmetadata file does not play well with mp so io lock is needed
                            report_queue.put(
                                ("INFO", f"Writing chip square {xarr_chip.shape} to {chip_data_path}... {square_name}"))
                            with chip_io_lock:
                                xarr_chip.to_zarr(
                                    store=chip_data_path, mode="a")
                            if xarr_anno is not None:
                                report_queue.put(
                                    ("INFO", f"Writing anno square {xarr_anno.shape} to {anno_data_path}... {square_name}"))
                                with ann_io_lock:
                                    xarr_anno.to_zarr(
                                        store=anno_data_path, mode="a")
                except Exception as e:
                    report_queue.put(
                        ("ERROR", f"Failed to write square to {chip_data_path}: {type(e)} {e} {square_name}"))
                    if self._file_type == "NPY":
                        try:
                            out_file.unlink(missing_ok=True)
                        except Exception as e:
                            report_queue.put(
                                ("ERROR", f"Failed to clean file square in {chip_data_path}: {type(e)} {e} {square_name}"))
            result_queue.put(square_name)

        result_queue.put(None)


def parse_args():
    parser = argparse.ArgumentParser(description='Sampler Arguments')
    parser.add_argument('--config', type=str)
    return vars(parser.parse_args())


def main(**kwargs):
    # TODO: add additional kwargs checks
    from settings import DOWNLOADER as config, SAMPLE_PATH
    os.makedirs(SAMPLE_PATH, exist_ok=True)

    if (config_path := kwargs["config"]) is not None:
        with open(config_path, "r") as f:
            config = config | yaml.safe_load(f)

    downloader = Downloader(**config)
    downloader.start()


if __name__ == "__main__":
    main(**parse_args())