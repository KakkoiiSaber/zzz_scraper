import sys
from pathlib import Path
from PIL import Image

def resize_and_pad(input_path):
	input_path = Path(input_path)
	img = Image.open(input_path)
	# Step 1: resize to 3840x2160
	img_resized = img.resize((3840, 2160), Image.LANCZOS)

	# Crop 3840x(0-1930) (from y=0 to y=1930)
	main_crop = img_resized.crop((0, 0, 3840, 1930))  # 3840x1930
	# Append 3840x(1870-2160) (from y=1870 to y=2160)
	append1 = img_resized.crop((0, 1870, 3840, 2160))        # 3840x290
	# Append 3840x((2160-60)-2160) (from y=2160-60 to y=2160)
	append2 = img_resized.crop((0, 2160-60, 3840, 2160))       # 3840x60
	# Append 3840x((2160-240)-2160) (from y=2160-240 to y=2160)
	# append3 = img_resized.crop((0, 2160-240, 3840, 2160))        # 3840x80

	# Stack them: main_crop + append1 + append2 + append3
	total_height = 2000 + 80 + 160 + 80  # 2320
	new_img = Image.new("RGB", (3840, total_height))
	y = 0
	new_img.paste(main_crop, (0, y)); y += 1930
	new_img.paste(append1, (0, y)); y += 290
	new_img.paste(append2, (0, y)); y += 60
	new_img.paste(append2, (0, y)); y += 60
	new_img.paste(append2, (0, y)); y += 60
    
	# new_img.paste(append3, (0, y)); y += 80

	# Output path
	out_path = input_path.parent / (input_path.stem + "_mac" + input_path.suffix)
	new_img.save(out_path)
	print(f"Saved: {out_path}")

if __name__ == "__main__":
    file_path = "downloads/米游社-官方资讯-minas/2025.09.01 《绝区零》2025年9月月历壁纸/9月月历壁纸_PC版.jpg"
    resize_and_pad(file_path)

