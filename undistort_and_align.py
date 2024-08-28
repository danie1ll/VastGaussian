import os
import logging
from argparse import ArgumentParser
import shutil

parser = ArgumentParser("Colmap converter")
parser.add_argument("--source_path", "-s", required=True, type=str)
parser.add_argument("--colmap_executable", default="", type=str)
args = parser.parse_args()
colmap_command = '"{}"'.format(args.colmap_executable) if len(args.colmap_executable) > 0 else "colmap"

# Execute model_orientation_aligner

# colmap model_orientation_aligner --input_path lisbon-300-colmap/sparse/0/ --image_path lisbon-300-colmap/images/ --output_path lisbon-300-colmap/sparse/0

model_aligner_cmd = (colmap_command + " model_orientation_aligner \
    --input_path " + args.source_path + "/colmap/sparse/0 \
    --image_path " + args.source_path + "/images \
    --output_path " + args.source_path + "/colmap/sparse/0")
exit_code = os.system(model_aligner_cmd)
if exit_code != 0:
    logging.error(f"Model orientation alignment failed with code {exit_code}. Exiting.")
    exit(exit_code)

### Image undistortion
## We need to undistort our images into ideal pinhole intrinsics.
output_path = os.path.join(args.source_path, "undistorted")
os.makedirs(output_path, exist_ok=True)

img_undist_cmd = (colmap_command + " image_undistorter \
    --image_path " + args.source_path + "/images \
    --input_path " + args.source_path + "/colmap/sparse/0 \
    --output_path " + output_path + " \
    --output_type COLMAP")
exit_code = os.system(img_undist_cmd)
if exit_code != 0:
    logging.error(f"image_undistorter failed with code {exit_code}. Exiting.")
    exit(exit_code)


files = os.listdir(output_path + "/sparse")
os.makedirs(output_path + "/sparse/0", exist_ok=True)
# Copy each file from the source directory to the destination directory
for file in files:
    if file == '0':
        continue
    source_file = os.path.join(output_path, "sparse", file)
    destination_file = os.path.join(output_path, "sparse", "0", file)
    shutil.move(source_file, destination_file)
