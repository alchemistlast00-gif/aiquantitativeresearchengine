#!/usr/bin/env python3
"""
gee_export_sentinel.py

Single-file script to:
 - Authenticate to Google Earth Engine
 - Pull Sentinel-2 (COPERNICUS/S2_SR_HARMONIZED) or Sentinel-1 (COPERNICUS/S1_GRD)
 - Apply QA60-based cloud masking for Sentinel-2 and filter by CLOUDY_PIXEL_PERCENTAGE
 - Create a median composite
 - Export the result to Google Drive at 10 m scale

Dependencies: earthengine-api
Install: pip install earthengine-api

Usage: edit the variables below or run from CLI:
    python gee_export_sentinel.py --mode S2 --out_name my_image --drive_folder "GEEExports"

Notes:
 - AOI_COORDS is a polygon given as a list of (lat, lon) tuples (user-friendly). The script converts to the (lon, lat) tuples Earth Engine expects.
 - For S2, cloud masking uses QA60 bits 10 and 11. Also images are filtered by the CLOUDY_PIXEL_PERCENTAGE metadata field.
"""

import ee
import argparse
import sys
import time

# -------------------- User-changeable defaults (top of file) --------------------
# Provide AOI as list of (lat, lon) tuples (easier to think in lat/lon). The script converts to EE expected (lon, lat).
AOI_COORDS = [
    (37.4215, -122.085),  # Example: near Googleplex (lat, lon)
    (37.423,  -122.085),
    (37.423,  -122.082),
    (37.4215, -122.082),
    (37.4215, -122.085),
]

START_DATE = "2022-06-01"
END_DATE = "2022-09-30"
CLOUD_PERCENTAGE_THRESHOLD = 20  # percent: filter images with CLOUDY_PIXEL_PERCENTAGE < this value

DEFAULT_MODE = "S2"  # "S2" or "S1"
EXPORT_SCALE = 10  # meters per pixel
EXPORT_MAX_PIXELS = 1e13

# Google Drive export settings (change if desired)
DEFAULT_DRIVE_FOLDER = "GEE_Exports"
DEFAULT_OUTPUT_NAME = "gee_export_median"

# -------------------- End of user-changeable section --------------------

def convert_aoi_latlon_to_ee_polygon(latlon_coords):
    """Convert list of (lat, lon) to list of [lon, lat] and return ee.Geometry.Polygon"""
    lonlat = [[(lon, lat) for (lat, lon) in latlon_coords]]
    return ee.Geometry.Polygon(lonlat)

def mask_s2_sr_clouds(image):
    """
    Cloud mask for Sentinel-2 SR using QA60 band:
    Bits: 10 = clouds, 11 = cirrus
    Keep pixels where both bits are 0.
    """
    qa = image.select('QA60')
    # Define bitmasks
    cloud_bit = 1 << 10
    cirrus_bit = 1 << 11
    mask = qa.bitwiseAnd(cloud_bit).eq(0).And(qa.bitwiseAnd(cirrus_bit).eq(0))
    return image.updateMask(mask)

def build_s2_collection(aoi, start_date, end_date, cloud_pct_thresh):
    """
    Return a filtered and cloud-masked Sentinel-2 SR Harmonized ImageCollection
    """
    col = (ee.ImageCollection("COPERNICUS/S2_SR_HARMONIZED")
           .filterBounds(aoi)
           .filterDate(start_date, end_date)
           # CLOUDY_PIXEL_PERCENTAGE is a top-level metadata property for S2 images
           .filter(ee.Filter.lt('CLOUDY_PIXEL_PERCENTAGE', cloud_pct_thresh))
          )
    # Map cloud mask
    col_masked = col.map(mask_s2_sr_clouds)
    return col_masked

def build_s1_collection(aoi, start_date, end_date):
    """
    Return a filtered Sentinel-1 GRD ImageCollection (typical options: IW mode, VV or VH).
    This example keeps VV polarization in IW mode and ascending+descending.
    """
    col = (ee.ImageCollection("COPERNICUS/S1_GRD")
           .filterBounds(aoi)
           .filterDate(start_date, end_date)
           .filter(ee.Filter.eq('instrumentMode', 'IW'))
           # Optionally filter to VV polarization; comment out to include all polarizations
           .filter(ee.Filter.listContains('transmitterReceiverPolarisation', 'VV'))
           # Use 'GRD' product type (usually default)
           .filter(ee.Filter.eq('productType', 'GRD'))
          )
    # Convert to linear scale if needed (S1 is in dB or linear depending on product). Here we leave as-is.
    return col

def export_to_drive(image, aoi, description, folder, filename_prefix, scale, max_pixels):
    """
    Export an ee.Image to Google Drive. Starts the task and prints status periodically.
    """
    # Use geometry.getInfo() for the region argument (list of coords). This will perform a small server call.
    try:
        region = aoi.getInfo()['coordinates']
    except Exception as e:
        print("Failed to get AOI geometry info for export region:", e)
        region = None

    task = ee.batch.Export.image.toDrive(
        image=image,
        description=description,
        folder=folder,
        fileNamePrefix=filename_prefix,
        region=region,
        scale=scale,
        maxPixels=int(max_pixels)
    )
    task.start()
    print(f"Export task started: {description}")
    print("Task id:", task.id)
    print("Check https://code.earthengine.google.com/tasks to monitor progress.")
    # Poll status briefly
    while True:
        status = task.status()
        state = status.get('state')
        print("Current task state:", state)
        if state in ('COMPLETED', 'FAILED', 'CANCELLED'):
            print("Task finished with state:", state)
            if 'error_message' in status:
                print("Error message:", status.get('error_message'))
            break
        time.sleep(10)

def main(args):
    # Initialize Earth Engine (authenticate if necessary)
    try:
        ee.Initialize()
    except Exception:
        print("Authenticating Earth Engine...")
        ee.Authenticate()
        ee.Initialize()

    # Convert AOI to ee.Geometry.Polygon (script expects AOI_COORDS as list of (lat, lon))
    aoi = convert_aoi_latlon_to_ee_polygon(args.aoi)

    mode = args.mode.upper()
    if mode == 'S2':
        print("Building Sentinel-2 SR collection...")
        col = build_s2_collection(aoi, args.start_date, args.end_date, args.cloud_thresh)
        # If user wants specific bands, choose them (S2 SR has e.g., B2, B3, B4, B8 etc). We'll keep all bands by default.
        composite = col.median().clip(aoi)
    elif mode == 'S1':
        print("Building Sentinel-1 GRD collection...")
        col = build_s1_collection(aoi, args.start_date, args.end_date)
        # For SAR, commonly convert to decibels: 10 * log10(image)
        # We'll compute median in linear scale, but since S1_GRD is already in dB for GRD-VV? Behavior may vary by product.
        composite = col.median().clip(aoi)
    else:
        raise ValueError("Unsupported mode. Use 'S2' or 'S1'.")

    # Print basic info
    count = col.size().getInfo()
    print(f"Number of images in filtered collection: {count}")

    # Kick off export
    export_to_drive(
        image=composite,
        aoi=aoi,
        description=f"{mode}_median_export_{args.out_name}",
        folder=args.drive_folder,
        filename_prefix=args.out_name,
        scale=args.scale,
        max_pixels=args.max_pixels
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Export median Sentinel imagery to Google Drive via Earth Engine.")
    parser.add_argument("--mode", default=DEFAULT_MODE, choices=['S2','S1','s2','s1'],
                        help="Which dataset to use: S2 or S1.")
    parser.add_argument("--start_date", default=START_DATE, help="Start date (YYYY-MM-DD)")
    parser.add_argument("--end_date", default=END_DATE, help="End date (YYYY-MM-DD)")
    parser.add_argument("--cloud_thresh", type=float, default=CLOUD_PERCENTAGE_THRESHOLD,
                        help="Max CLOUDY_PIXEL_PERCENTAGE (for S2).")
    parser.add_argument("--drive_folder", default=DEFAULT_DRIVE_FOLDER,
                        help="Google Drive folder to export to.")
    parser.add_argument("--out_name", default=DEFAULT_OUTPUT_NAME, help="Output filename prefix.")
    parser.add_argument("--scale", type=int, default=EXPORT_SCALE, help="Export scale in meters.")
    parser.add_argument("--max_pixels", type=float, default=EXPORT_MAX_PIXELS, help="maxPixels for export.")
    # Allow overriding AOI via command-line: provide as a semicolon-separated list of lat,lon pairs:
    parser.add_argument("--aoi", default=None,
                        help=("AOI polygon as semicolon-separated lat,lon pairs, e.g. "
                              "\"37.42,-122.085;37.423,-122.085;37.423,-122.082;37.4215,-122.082;37.4215,-122.085\" "
                              "If omitted, uses the AOI_COORDS hardcoded at top of this file."))

    parsed = parser.parse_args()

    # Parse AOI argument if provided
    if parsed.aoi:
        try:
            coords = []
            for pair in parsed.aoi.split(';'):
                lat_s, lon_s = pair.split(',')
                lat = float(lat_s.strip())
                lon = float(lon_s.strip())
                coords.append((lat, lon))
            parsed.aoi = coords
        except Exception as e:
            print("Failed to parse --aoi argument:", e)
            sys.exit(1)
    else:
        parsed.aoi = AOI_COORDS

    main(parsed)
