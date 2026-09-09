"""Climate entity for a Midea 0xB2 air conditioner behind a ZHA Zigbee IR blaster.

Every frame is computed from `protocol.py` at send time. There is no code table: the whole
state space is a function of (mode, temperature, fan), so storing it would be storing
arithmetic.

IR is one-way. Nothing reports back, so this entity's state is what was last *sent*, not
what the unit is doing — someone using the handset desynchronises it silently. State is
restored across restarts rather than assumed to be off.
"""

from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol
from homeassistant.components.climate import (
    PLATFORM_SCHEMA,
    ClimateEntity,
    ClimateEntityFeature,
    HVACMode,
)
from homeassistant.const import ATTR_TEMPERATURE, CONF_NAME, CONF_UNIQUE_ID, UnitOfTemperature
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.event import async_track_state_change_event
from homeassistant.helpers.restore_state import RestoreEntity

from .protocol import (
    FAN_MODES,
    MAX_TEMP_F,
    MIN_TEMP_F,
    ProtocolError,
    code_for,
    command_code,
)

_LOGGER = logging.getLogger(__name__)

CONF_IEEE = "ieee"
CONF_ENDPOINT_ID = "endpoint_id"
CONF_CLUSTER_ID = "cluster_id"
CONF_TEMPERATURE_SENSOR = "temperature_sensor"
CONF_HUMIDITY_SENSOR = "humidity_sensor"

DEFAULT_NAME = "Midea IR AC"
DEFAULT_ENDPOINT_ID = 1
DEFAULT_CLUSTER_ID = 0xE004  # ZosungIRControl
IRSEND_COMMAND = 2

SWING_ON = "on"
SWING_OFF = "off"
PRESET_NONE = "none"
PRESET_TURBO = "turbo"

HVAC_MODES = [
    HVACMode.OFF,
    HVACMode.COOL,
    HVACMode.HEAT,
    HVACMode.DRY,
    HVACMode.AUTO,
    HVACMode.FAN_ONLY,
]

PLATFORM_SCHEMA = PLATFORM_SCHEMA.extend(
    {
        vol.Optional(CONF_NAME, default=DEFAULT_NAME): cv.string,
        vol.Optional(CONF_UNIQUE_ID): cv.string,
        vol.Required(CONF_IEEE): cv.string,
        vol.Optional(CONF_ENDPOINT_ID, default=DEFAULT_ENDPOINT_ID): cv.positive_int,
        vol.Optional(CONF_CLUSTER_ID, default=DEFAULT_CLUSTER_ID): cv.positive_int,
        vol.Optional(CONF_TEMPERATURE_SENSOR): cv.entity_id,
        vol.Optional(CONF_HUMIDITY_SENSOR): cv.entity_id,
    }
)


async def async_setup_platform(hass, config, async_add_entities, discovery_info=None):
    async_add_entities([ZhaMideaIrClimate(hass, config)])


class ZhaMideaIrClimate(ClimateEntity, RestoreEntity):
    """A Midea 0xB2 air conditioner driven through ZHA's cluster-command service."""

    _attr_should_poll = False
    _attr_temperature_unit = UnitOfTemperature.FAHRENHEIT
    _attr_hvac_modes = HVAC_MODES
    _attr_fan_modes = list(FAN_MODES)
    _attr_swing_modes = [SWING_OFF, SWING_ON]
    _attr_preset_modes = [PRESET_NONE, PRESET_TURBO]
    _attr_min_temp = MIN_TEMP_F
    _attr_max_temp = MAX_TEMP_F
    _attr_target_temperature_step = 1
    _attr_supported_features = (
        ClimateEntityFeature.TARGET_TEMPERATURE
        | ClimateEntityFeature.FAN_MODE
        | ClimateEntityFeature.SWING_MODE
        | ClimateEntityFeature.PRESET_MODE
        | ClimateEntityFeature.TURN_ON
        | ClimateEntityFeature.TURN_OFF
    )

    def __init__(self, hass: HomeAssistant, config: dict[str, Any]) -> None:
        self.hass = hass
        self._attr_name = config[CONF_NAME]
        self._attr_unique_id = config.get(CONF_UNIQUE_ID)

        self._ieee = config[CONF_IEEE]
        self._endpoint_id = config[CONF_ENDPOINT_ID]
        self._cluster_id = config[CONF_CLUSTER_ID]

        self._temperature_sensor = config.get(CONF_TEMPERATURE_SENSOR)
        self._humidity_sensor = config.get(CONF_HUMIDITY_SENSOR)

        self._attr_hvac_mode = HVACMode.OFF
        self._attr_fan_mode = FAN_MODES[0]
        self._attr_swing_mode = SWING_OFF
        self._attr_preset_mode = PRESET_NONE
        self._attr_target_temperature = 72
        # The mode to return to when turned on without one being named.
        self._last_on_mode = HVACMode.COOL

    # --- lifecycle ---------------------------------------------------------------------

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()

        if (last := await self.async_get_last_state()) is not None:
            if last.state in HVAC_MODES:
                self._attr_hvac_mode = HVACMode(last.state)
                if last.state != HVACMode.OFF:
                    self._last_on_mode = HVACMode(last.state)
            attrs = last.attributes
            if (temp := attrs.get(ATTR_TEMPERATURE)) is not None:
                self._attr_target_temperature = float(temp)
            if (fan := attrs.get("fan_mode")) in FAN_MODES:
                self._attr_fan_mode = fan
            if (swing := attrs.get("swing_mode")) in (SWING_ON, SWING_OFF):
                self._attr_swing_mode = swing
            if (preset := attrs.get("preset_mode")) in (PRESET_NONE, PRESET_TURBO):
                self._attr_preset_mode = preset

        tracked = [e for e in (self._temperature_sensor, self._humidity_sensor) if e]
        if tracked:
            self.async_on_remove(
                async_track_state_change_event(self.hass, tracked, self._async_sensor_changed)
            )

    # --- sensor mirroring ---------------------------------------------------------------
    #
    # Read live rather than cached. Caching at async_added_to_hass loses the race against a
    # radio sensor that is still `unavailable` at startup, and then holds None until that
    # sensor next changes -- which is exactly what happened on the first deploy, leaving
    # temperature empty while humidity happened to be up in time.

    def _sensor_value(self, entity_id: str | None) -> float | None:
        if not entity_id:
            return None
        state = self.hass.states.get(entity_id)
        if state is None:
            return None
        try:
            return float(state.state)
        except (TypeError, ValueError):
            return None

    @property
    def current_temperature(self) -> float | None:
        return self._sensor_value(self._temperature_sensor)

    @property
    def current_humidity(self) -> float | None:
        return self._sensor_value(self._humidity_sensor)

    @callback
    def _async_sensor_changed(self, event) -> None:
        self.async_write_ha_state()

    # --- transmission -------------------------------------------------------------------

    async def _send(self, code: str) -> None:
        await self.hass.services.async_call(
            "zha",
            "issue_zigbee_cluster_command",
            {
                "ieee": self._ieee,
                "endpoint_id": self._endpoint_id,
                "cluster_id": self._cluster_id,
                "cluster_type": "in",
                "command": IRSEND_COMMAND,
                "command_type": "server",
                # Bare base64. The ZHA quirk builds its own {"key_num":1,...} envelope, and
                # pre-wrapping nests one inside the other, whereupon the blaster transmits
                # nothing at all: no error, no LED, no IR.
                "params": {"code": code},
            },
            blocking=True,
        )

    async def _send_state(self) -> None:
        try:
            code = code_for(
                self._attr_hvac_mode.value,
                self._attr_target_temperature,
                self._attr_fan_mode,
            )
        except ProtocolError as err:
            _LOGGER.error("%s: cannot encode that state: %s", self._attr_name, err)
            return
        await self._send(code)

    # --- commands -----------------------------------------------------------------------

    async def async_set_hvac_mode(self, hvac_mode: HVACMode) -> None:
        self._attr_hvac_mode = hvac_mode
        if hvac_mode != HVACMode.OFF:
            self._last_on_mode = hvac_mode
        await self._send_state()
        self.async_write_ha_state()

    async def async_turn_on(self) -> None:
        await self.async_set_hvac_mode(self._last_on_mode)

    async def async_turn_off(self) -> None:
        await self.async_set_hvac_mode(HVACMode.OFF)

    async def async_set_temperature(self, **kwargs: Any) -> None:
        if (temperature := kwargs.get(ATTR_TEMPERATURE)) is None:
            return
        self._attr_target_temperature = float(temperature)
        if (mode := kwargs.get("hvac_mode")) is not None:
            self._attr_hvac_mode = HVACMode(mode)
            if self._attr_hvac_mode != HVACMode.OFF:
                self._last_on_mode = self._attr_hvac_mode
        if self._attr_hvac_mode != HVACMode.OFF:
            await self._send_state()
        self.async_write_ha_state()

    async def async_set_fan_mode(self, fan_mode: str) -> None:
        self._attr_fan_mode = fan_mode
        if self._attr_hvac_mode != HVACMode.OFF:
            await self._send_state()
        self.async_write_ha_state()

    async def async_set_swing_mode(self, swing_mode: str) -> None:
        # Swing is its own command frame, not a bit in the state frame, so it is sent on its
        # own and does not need the state re-sending afterwards.
        self._attr_swing_mode = swing_mode
        await self._send(
            command_code("swing_on" if swing_mode == SWING_ON else "swing_off")
        )
        self.async_write_ha_state()

    async def async_set_preset_mode(self, preset_mode: str) -> None:
        self._attr_preset_mode = preset_mode
        await self._send(
            command_code("turbo_on" if preset_mode == PRESET_TURBO else "turbo_off")
        )
        self.async_write_ha_state()
