# Convert one 8-day time step of the gridded dcSIF product (0.05 deg) into a
# Cloud-Optimized GeoTIFF + STAC item that openEO load_stac can read.
#
# Data: Global 0.05 deg 8-day Gridded TROPOMI SIF (dcSIF) from ANN-based Retrievals,
#       May 2018 - July 2025. Zenodo, doi:10.5281/zenodo.22766916
#       https://zenodo.org/records/22766916  (one NetCDF file per year)
#
# Run from the repository root (SIF_downscaling_CDSE/), either cell by cell (# %%)
# or as a whole:  python src/convert_dcsif_to_stac.py
# The STAC item format follows the working examples in the data directory.

# %% Imports
import os
import json
import hashlib
from datetime import datetime, timedelta

import numpy as np
import requests
import xarray as xr
import rioxarray  # noqa: F401  (registers the .rio accessor)
import pystac
from pystac.extensions.projection import ProjectionExtension
from pystac.extensions.eo import EOExtension
from shapely.geometry import box, mapping

# %% Settings
YEAR = 2023
WINDOW_START = "2023-07-20"  # time label of the 8-day window (window start)
WINDOW_DAYS = 8

# Yearly NetCDF files are ~1.2 GB: keep them OUTSIDE the repository so they are never committed.
NC_DIR = "../dcSIF_zenodo"
NC_NAME = f"TROPOMI_ANN_dcSIF_0d05deg_8day_{YEAR}.nc"
NC_PATH = os.path.join(NC_DIR, NC_NAME)
NC_URL = f"https://zenodo.org/records/22766916/files/{NC_NAME}?download=1"
NC_MD5 = {  # from the Zenodo record
    2018: "d7e2f5ecebb35606ca9c4adbe0c7d933",
    2019: "ee32c2453de363581c0cdf5bb15ffc98",
    2020: "446f2b472ba0af08cc5f5508c48ecb8a",
    2021: "a2730230230fe8841dd14ec7e6aaff31",
    2022: "a1363f28adbca99ae745aa9454077dae",
    2023: "5741de80436bbd2ad2c014b149ecedfc",
    2024: "275962792687e3bc9586a6278719ed02",
    2025: "0b2fb62670e5c4d92599b62f77a4068e",
}
VAR = "sif_dc"

# Repository that will host the GeoTIFF (change to your fork when testing)
GITHUB_USER = "dpabon"
REGION = "pannonian_basin"

spatial_extent = {"west": 16.0, "east": 23.0, "south": 45.5, "north": 48.5}
BUFFER_DEG = 1.0  # extra margin around the area of interest

NODATA = -9999.0

t0 = datetime.strptime(WINDOW_START, "%Y-%m-%d")
t1 = t0 + timedelta(days=WINDOW_DAYS - 1)
tag = f"{t0:%Y%m%d}_{t1:%Y%m%d}"

out_dir = f"data/{t0:%Y-%m}-dcSIF"
os.makedirs(out_dir, exist_ok=True)
f_tif = f"{out_dir}/dcSIF_8day_{tag}_{REGION}.tif"
f_json = f"{out_dir}/dcSIF_8day_{tag}_{REGION}.json"

tif_url = f"https://github.com/{GITHUB_USER}/SIF_downscaling_CDSE/raw/refs/heads/main/{f_tif}"
json_url = f"https://raw.githubusercontent.com/{GITHUB_USER}/SIF_downscaling_CDSE/refs/heads/main/{f_json}"


# %% Download the yearly NetCDF from Zenodo (skipped if already present)
def md5sum(path, chunk=2**24):
    h = hashlib.md5()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(chunk), b""):
            h.update(block)
    return h.hexdigest()


if not os.path.exists(NC_PATH):
    os.makedirs(NC_DIR, exist_ok=True)
    print(f"Downloading {NC_URL} -> {NC_PATH}")
    with requests.get(NC_URL, stream=True, timeout=600) as r:
        r.raise_for_status()
        with open(NC_PATH + ".part", "wb") as f:
            for block in r.iter_content(chunk_size=2**24):
                f.write(block)
    os.replace(NC_PATH + ".part", NC_PATH)
else:
    print(f"Found {NC_PATH}, skipping download.")

if YEAR in NC_MD5:
    ok = md5sum(NC_PATH) == NC_MD5[YEAR]
    print(f"MD5 check: {'OK' if ok else 'MISMATCH'}")
    if not ok:
        raise ValueError(f"MD5 mismatch for {NC_PATH}; delete it and download again.")

# %% Open NetCDF lazily and select the time step
ds = xr.open_dataset(NC_PATH)  # lazy: only the selected subset is read
print(ds)

sel_time = ds.time.sel(time=np.datetime64(WINDOW_START), method="nearest").values
if abs(sel_time - np.datetime64(WINDOW_START)) > np.timedelta64(1, "D"):
    raise ValueError(f"No time step close to {WINDOW_START}; nearest is {sel_time}")
print(f"Selected time step: {sel_time}")

# %% Subset AOI (+ buffer), orient north-up, set CRS and nodata
west = spatial_extent["west"] - BUFFER_DEG
east = spatial_extent["east"] + BUFFER_DEG
south = spatial_extent["south"] - BUFFER_DEG
north = spatial_extent["north"] + BUFFER_DEG

da = ds[VAR].sel(time=sel_time)
da = da.sel(lon=slice(west, east))
if da.lat.values[0] < da.lat.values[-1]:
    da = da.sel(lat=slice(south, north))
else:
    da = da.sel(lat=slice(north, south))

da = da.transpose("lat", "lon").load()
da = da.sortby("lat", ascending=False)  # GeoTIFF rows go north -> south
da = da.rename({"lat": "y", "lon": "x"}).drop_vars("time", errors="ignore")
da = da.astype("float32")
# float32 coordinates cause tiny offsets in the bounds (e.g. 14.9999996); round them
da = da.assign_coords(
    x=np.round(da.x.values.astype("float64"), 4),
    y=np.round(da.y.values.astype("float64"), 4),
)

# %% Quick checks (use these numbers to set param_min / param_ini / param_max)
vals = da.values
valid = np.isfinite(vals)
print(f"Grid shape (y, x): {vals.shape}")
print(f"Resolution: {float(abs(da.x[1] - da.x[0])):.4f} x {float(abs(da.y[1] - da.y[0])):.4f} deg")
print(f"Valid fraction (AOI + buffer): {valid.mean():.2f}")
print("dcSIF percentiles 1/5/50/95/99:",
      np.round(np.nanpercentile(vals, [1, 5, 50, 95, 99]), 3))
print(f"Negative values fraction: {(vals[valid] < 0).mean():.3f}")

# %% Write GeoTIFF (COG)
da = da.fillna(NODATA)
da = da.rio.write_crs("EPSG:4326")
da = da.rio.write_nodata(NODATA)
da.rio.to_raster(f_tif, driver="COG", compress="DEFLATE")
print(f"Saved GeoTIFF: {f_tif}")

# %% STAC item (same structure as the working examples)
bbox = [float(b) for b in da.rio.bounds()]
shape = [int(da.rio.height), int(da.rio.width)]
t_mid = t0 + timedelta(days=WINDOW_DAYS / 2)

item = pystac.Item(
    id=f"dcSIF_8day_{tag}_{REGION}",
    geometry=mapping(box(*bbox)),
    bbox=bbox,
    datetime=t_mid,
    properties={
        "start_datetime": f"{t0:%Y-%m-%d}T00:00:00Z",
        "end_datetime": f"{t1:%Y-%m-%d}T23:59:59Z",
    },
)

proj_ext = ProjectionExtension.ext(item, add_if_missing=True)
proj_ext.epsg = 4326
proj_ext.shape = shape
proj_ext.bbox = bbox

EOExtension.ext(item, add_if_missing=True)

item.add_asset(
    key="SIF",
    asset=pystac.Asset(
        href=tif_url,
        title=f"dcSIF 8-day composite {t0:%Y-%m-%d} to {t1:%Y-%m-%d} (0.05 deg)",
        media_type="image/tiff; application=geotiff; profile=cloud-optimized",
        extra_fields={"eo:bands": [{"name": "SIF", "nodata": NODATA}]},
    ),
)

item_dict = item.to_dict()

# Match the projection fields of the working examples (proj:epsg, extension v1.1.0);
# newer pystac versions write proj:code instead, which the backend may not read.
item_dict["properties"].pop("proj:code", None)
item_dict["properties"]["proj:epsg"] = 4326
item_dict["stac_extensions"] = [
    "https://stac-extensions.github.io/eo/v1.1.0/schema.json",
    "https://stac-extensions.github.io/projection/v1.1.0/schema.json",
]

with open(f_json, "w") as f:
    json.dump(item_dict, f, indent=2)
print(f"Saved STAC item: {f_json}")

# %% Next steps (run manually in the terminal when ready)
print("\nTo publish, run in the terminal:")
print(f"  git add {f_tif} {f_json}")
print(f'  git commit -m "Add dcSIF 8-day {tag} {REGION}" {f_tif} {f_json}')
print("  git push")
print("\nThen use this URL in load_stac:")
print(f"  {json_url}")