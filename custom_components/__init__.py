"""VNExpress News custom component for Home Assistant."""
import logging

_LOGGER = logging.getLogger(__name__)

DOMAIN = "vnexpress_news"

async def async_setup(hass, config):
    """Set up the VNExpress News component."""
    _LOGGER.info("Khởi tạo VNExpress News component")
    _LOGGER.debug("Bắt đầu thiết lập component vnexpress_news")
    return True