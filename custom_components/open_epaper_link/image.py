"""Image platform for OpenEPaperLink integration."""
from __future__ import annotations

import logging
import os

from homeassistant.components.image import ImageEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN, SIGNAL_TAG_IMAGE_UPDATE
from .hub import Hub
from .tag_types import get_hw_string
from .util import get_image_path

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up OpenEPaperLink ImageEntities from a config entry."""
    hub = hass.data[DOMAIN][entry.entry_id]

    # Use a set to track MACs of added entities to prevent duplicates
    added_entities = set()

    @callback
    def async_add_image_entity(tag_mac: str) -> None:
        """Add an image entity for a new or existing tag."""
        if tag_mac in added_entities:
            return

        # Do not create entities for the AP or blacklisted tags
        if tag_mac == "ap" or tag_mac in hub.get_blacklisted_tags():
            return

        entity = OpenEPaperLinkImageEntity(hub, tag_mac)
        async_add_entities([entity])
        added_entities.add(tag_mac)

    # Add entities for all tags known at setup time
    for tag_mac in hub.tags:
        async_add_image_entity(tag_mac)

    # Listen for newly discovered tags and add them dynamically
    entry.async_on_unload(
        async_dispatcher_connect(
            hass, f"{DOMAIN}_tag_discovered", async_add_image_entity)
    )


class OpenEPaperLinkImageEntity(ImageEntity):
    """Represents an ImageEntity for an OpenEPaperLink tag."""

    _attr_has_entity_name = True
    _attr_translation_key = "content"
    _attr_icon = "mdi:image"

    def __init__(self, hub: Hub, tag_mac: str) -> None:
        """Initialize the image entity."""
        super().__init__()
        self._hub = hub
        self._tag_mac = tag_mac
        self._attr_unique_id = f"{tag_mac}_content"

        tag_data = hub.get_tag_data(tag_mac)
        hw_type = tag_data.get("hw_type", 0)

        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, self._tag_mac)},
            name=tag_data.get("tag_name", tag_mac),
            manufacturer="OpenEPaperLink",
            model=get_hw_string(hw_type),
            via_device=(DOMAIN, "ap"),
        )
        # Path is initialized later in async_image to ensure hass is available
        self._image_path: str | None = None

    @property
    def available(self) -> bool:
        """Return True if the entity is available."""
        return (
            self._hub.online
            and self._tag_mac in self._hub.tags
            and self._tag_mac not in self._hub.get_blacklisted_tags()
        )

    async def async_image(self) -> bytes | None:
        """Return the image bytes."""
        if self._image_path is None:
            self._image_path = get_image_path(
                self.hass, f"{DOMAIN}.{self._tag_mac}")

        if not os.path.exists(self._image_path):
            return None

        try:
            return await self.hass.async_add_executor_job(
                lambda: open(self._image_path, "rb").read()
            )
        except OSError as err:
            _LOGGER.error("Could not read image file for %s: %s",
                          self.entity_id, err)
            return None

    async def async_added_to_hass(self) -> None:
        """Register callbacks when the entity is added to hass."""
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass,
                f"{SIGNAL_TAG_IMAGE_UPDATE}_{self._tag_mac}",
                self.async_write_ha_state,
            )
        )
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass,
                f"{DOMAIN}_connection_status",
                self.async_write_ha_state,
            )
        )
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass,
                f"{DOMAIN}_blacklist_update",
                self._handle_blacklist_update,
            )
        )

    @callback
    def _handle_blacklist_update(self) -> None:
        """Handle blacklist updates.

        If the tag is now blacklisted, it will be marked as unavailable.
        Home Assistant will remove it from the UI if it's unavailable for a certain period.
        A more direct approach would be self.async_remove(), but making it unavailable
        is often sufficient and safer.
        """
        self.async_write_ha_state()
