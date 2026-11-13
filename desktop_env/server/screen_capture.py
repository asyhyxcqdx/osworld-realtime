"""Request-local PNG capture for the Linux/X11 observer runtime."""
import io
import os

from PIL import Image, ImageGrab

try:
    from .pyxcursor import Xcursor
except ImportError:  # VM entry point imports modules from /home/user/server.
    from pyxcursor import Xcursor


def capture_png():
    """Return a fresh screenshot with cursor, without a shared file or buffer."""
    with Xcursor() as cursor:
        with Image.fromarray(cursor.getCursorImageArrayFast()) as cursor_image:
            # Explicit X11 capture keeps Pillow on its in-memory XCB backend;
            # failures propagate instead of using an external screenshot utility.
            with ImageGrab.grab(xdisplay=os.environ.get('DISPLAY', ':0.0')) as screenshot:
                # The cursor response already contains its position. Do not
                # query PyAutoGUI's shared Xlib connection from capture threads.
                screenshot.paste(cursor_image, cursor.position, cursor_image)
                with io.BytesIO() as buffer:
                    screenshot.save(buffer, format='PNG')
                    return buffer.getvalue()
