"""
Generates a QR code (as SVG markup) pointing at this board's dashboard URL,
so it can be printed/stuck on the physical device - "point your phone at
the desk gadget" is a much better discovery story than typing an IP.

Uses the board's LAN IP (found by asking the kernel which local interface
would route outbound traffic - no packets are actually sent) rather than
mDNS, since mDNS resolution isn't reliably available on every client network.
SVG output (via qrcode's SvgPathImage factory) avoids needing Pillow - one
less dependency on a board where we've already had enough dependency drama.
"""
import socket
import logging

import qrcode
import qrcode.image.svg

logger = logging.getLogger("workspace_ai.qr")


def _lan_ip() -> str:
    """Ask the kernel which local interface would route outbound traffic.
    No packets are sent — this works with no internet connection."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    except Exception:
        return "127.0.0.1"
    finally:
        s.close()


def get_dashboard_url(port: int) -> str:
    return f"http://{_lan_ip()}:{port}"


def generate_qr_svg(url: str) -> str:
    qr = qrcode.QRCode(image_factory=qrcode.image.svg.SvgPathImage, border=2)
    qr.add_data(url)
    qr.make()
    img = qr.make_image()

    import io
    buf = io.BytesIO()
    img.save(buf)
    return buf.getvalue().decode("utf-8")
