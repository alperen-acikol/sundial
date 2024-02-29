import lightning as L
import os
import torch
import xarray as xr

from torch import nn
from torch.utils.data import Dataset, DataLoader
from typing import Literal

from pipeline.settings import DATALOADER as config, STRATA_ATTR_NAME, STRATA_MAP_PATH


class PreprocesNormalization(nn.Module):
    def __init__(self, means, stds):
        super().__init__()
        self.means = torch.tensor(
            means, dtype=torch.float).view(1, 1, 1, -1)
        self.stds = torch.tensor(stds, dtype=torch.float).view(1, 1, 1, -1)

    def forward(self, x):
        return (x - self.means) / self.stds


class ChipsDataset(Dataset):
    def __init__(self,
                 means: list[float] | None,
                 stds: list[float] | None,
                 file_type: str,
                 chip_data_path: str,
                 anno_data_path: str,
                 sample_path: str,
                 chip_size: int,
                 base_year: int | None,
                 back_step: int | None,
                 include_names: bool = False,
                 **kwargs):
        super().__init__(**kwargs)
        self.file_type = file_type
        self.chip_data_path = chip_data_path
        self.anno_data_path = anno_data_path
        self.sample_path = sample_path
        self.chip_size = chip_size
        self.base_year = base_year
        self.back_step = back_step
        self.include_names = include_names
        self.normalize = PreprocesNormalization(
            means, stds) if means and stds else None

        self.image_loader = self._zarr_loader if self.file_type == "zarr" else self._zarr_loader

    def clip_chip(self, xarr):
        x_diff = xarr["x"].size - self.chip_size
        y_diff = xarr["y"].size - self.chip_size

        x_start = x_diff // 2
        x_end = x_diff - x_start

        y_start = y_diff // 2
        y_end = y_diff - y_start
        return xarr.sel(x=slice(x_start, -x_end), y=slice(y_start, -y_end))

    def get_strata(self, name):
        strata = self.image_loader(self.anno_data_path, name)
        if self.chip_size < max(strata["x"].size, strata["y"].size):
            strata = self.clip_chip(strata)
        return torch.as_tensor(strata.to_numpy(), dtype=torch.float)

    def slice_year(self, xarr: xr.Dataset, year: int):
        end_year = int(year) - self.base_year
        start_year = end_year - self.back_step
        return xarr.sel(year=slice(start_year, end_year+1))

    def __getitem__(self, idx):
        # loading image into xarr file
        paths = xr.open_zarr(self.sample_path)
        name = paths["square_name"].isel(index=idx).values.item()
        year = paths["year"].isel(index=idx).values.item()
        image = self.image_loader(self.chip_data_path, name)

        # slicing to target year if chip is larger and back_step is set
        if self.base_year is not None and self.back_step is not None:
            chip = self.slice_year(image, year)
        else:
            chip = image

        # clipping chip if larger than chip_size
        if self.chip_size < max(chip["x"].size, chip["y"].size):
            chip = self.clip_chip(chip)

        # converting to tensor
        chip = torch.as_tensor(chip.to_numpy(), dtype=torch.float)
        item = [chip]

        # normalizing chip to precalculated means and stds
        if self.normalize is not None:
            chip = self.normalize(chip)

        # including annotations if anno_data_path is set
        if self.anno_data_path is not None:
            strata = self.get_strata(name)
            item.append(strata)

        # including name if include_names is set
        if self.include_names:
            item.append(name)

        return item

    def __len__(self):
        paths = xr.open_zarr(self.sample_path)
        return paths["index"].size

    def _zarr_loader(self, data_path: str, name: int, **kwargs):
        image = xr.open_zarr(data_path)[name]
        return image

    def _tif_loader(self, data_path: str, name: int, **kwargs):
        image_path = os.path.join(data_path, f"{name}.tif")
        image = xr.open_rasterio(image_path)
        # TODO: convert to tensor
        return image


class ChipsDataModule(L.LightningDataModule):
    def __init__(
        self,
        batch_size: int,
        num_workers: int,
        means: list[float] | None,
        stds: list[float] | None,
        file_type: str = config["file_type"],
        chip_data_path: str = config["chip_data_path"],
        anno_data_path: str = config["anno_data_path"],
        train_sample_path: str = config["train_sample_path"],
        validate_sample_path: str = config["validate_sample_path"],
        test_sample_path: str = config["test_sample_path"],
        predict_sample_path: str = config["predict_sample_path"],
        chip_size: int = config["chip_size"],
        base_year: int = config["base_year"],
        back_step: int = config["back_step"],
        **kwargs
    ):
        super().__init__(**kwargs)
        self.batch_size = batch_size
        self.num_workers = num_workers
        self.means = means
        self.stds = stds
        self.file_type = file_type.lower()
        self.chip_data_path = chip_data_path
        self.anno_data_path = anno_data_path
        self.train_sample_path = train_sample_path
        self.validate_sample_path = validate_sample_path
        self.test_sample_path = test_sample_path
        self.predict_sample_path = predict_sample_path
        self.chip_size = chip_size
        self.base_year = base_year
        self.back_step = back_step
        self.normalize = None

        self.dataset_config = {
            "means": self.means,
            "stds": self.stds,
            "file_type": self.file_type,
            "chip_data_path": self.chip_data_path,
            "anno_data_path": self.anno_data_path,
            "chip_size": self.chip_size,
            "base_year": self.base_year,
            "back_step": self.back_step,
        }

        self.dataloader_config = {
            "batch_size": self.batch_size,
            "num_workers": self.num_workers,
            "pin_memory": True,
            "drop_last": True,
        }

    def setup(self, stage: Literal["fit", "validate", "test", "predict"]) -> None:
        match stage:
            case "fit":
                self.training_ds = ChipsDataset(
                    sample_path=self.train_sample_path,
                    **self.dataset_config)

                self.validate_ds = ChipsDataset(
                    sample_path=self.validate_sample_path,
                    **self.dataset_config)

            case "validate":
                self.validate_ds = ChipsDataset(
                    sample_path=self.validate_sample_path,
                    **self.dataset_config)

            case "test":
                self.test_ds = ChipsDataset(
                    sample_path=self.test_sample_path,
                    **self.dataset_config)

            case "predict":
                self.predict_ds = ChipsDataset(
                    sample_path=self.predict_sample_path,
                    **self.dataset_config | {
                        "anno_data_path": None,
                        "include_names": True,
                    })

    def train_dataloader(self):
        return DataLoader(
            dataset=self.training_ds,
            **self.dataloader_config
        )

    def val_dataloader(self):
        return DataLoader(
            dataset=self.validate_ds,
            **self.dataloader_config
        )

    def test_dataloader(self):
        return DataLoader(
            dataset=self.test_ds,
            **self.dataloader_config
        )

    def predict_dataloader(self):
        return DataLoader(
            dataset=self.predict_ds,
            **self.dataloader_config | {"drop_last": False}
        )