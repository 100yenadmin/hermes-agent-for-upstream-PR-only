"""Generate a synthetic image fixture without user or customer data."""
import sys
from PIL import Image, ImageDraw

image = Image.new("RGB", (256, 128), "white")
draw = ImageDraw.Draw(image)
draw.rectangle((16, 16, 110, 110), fill="red")
draw.ellipse((145, 24, 225, 104), fill="blue")
image.save(sys.argv[1])
