"""Pentair IntelliCenter: Climate platform

This climate entity replaces the legacy water_heater entity so we can:
- Offer a simple OFF / AUTO toggle (no explicit COOL mode)
- Use *presets* to request Pentair body modes (Gas, Solar, UltraTemp, Hybrid, etc.) 
  as custom HVAC modes are not supported
- Render actions from the panel's HTMODE (heating/cooling/idle)
- Dynamically switch between single setpoint (heat-only) and range setpoints
  (UltraTemp Only/Preferred) at runtime.

Notes
-----
* We rely on the controller's logic; we DO NOT toggle any "COOL" flags on heaters.
* Body.MODE (client->server) selects the strategy; Body.HTMODE (server->client)
  reports what's actually happening now.

This file mirrors the patterns used in water_heater.py (PoolEntity, controller/model,
requestChanges, attribute constants), but exposes a Climate entity instead.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from homeassistant.components.climate import ClimateEntity, ClimateEntityFeature
from homeassistant.components.climate.const import HVACAction, HVACMode
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import ATTR_TEMPERATURE
from homeassistant.core import HomeAssistant
from homeassistant.helpers.restore_state import RestoreEntity

from . import PoolEntity
from .const import DOMAIN
from .pyintellicenter import (
    BODY_TYPE,
    HEATER_TYPE,
    # Attribute keys (mirror attributes.py used by the integration)
    LISTORD_ATTR,
    SNAME_ATTR,
    STATUS_ATTR,
    MODE_ATTR,
    SUBTYP_ATTR,
    HTMODE_ATTR,
    LOTMP_ATTR,
    HITMP_ATTR,
    LSTTMP_ATTR,
    HEATER_ATTR,
    NULL_OBJNAM,
    ModelController,
    PoolObject,
)

_LOGGER = logging.getLogger(__name__)

# Raw Pentair field names used in BODY dicts that may not have *_ATTR constants
HTSRC_FIELD = "HTSRC"    # Body: which heater is actually driving now

# Heater fields
HEATER_COOL_FIELD = "COOL"  # Capability flag: "ON" if that heater can chill
HEATER_BODY_FIELD = "BODY"  # Space-separated BODY objnams the heater can serve

    
# Handle missing keys gracefully    
def _po_val(obj, key: str, default=None):
    try:
        return obj[key]
    except Exception:
        return default

# -----------------------------
# Body.HTMODE (status -> action)
# -----------------------------
HTMODE_TO_ACTION: Dict[int, HVACAction] = {
    0: HVACAction.IDLE,
    1: HVACAction.HEATING,  # Gas
    2: HVACAction.HEATING,  # Solar
    3: HVACAction.HEATING,  # Heat Pump
    4: HVACAction.HEATING,  # UltraTemp heating
    5: HVACAction.HEATING,  # Hybrid
    6: HVACAction.HEATING,  # MasterTemp
    7: HVACAction.HEATING,  # Max-E-Therm
    8: HVACAction.HEATING,  # ETI250
    9: HVACAction.COOLING,  # UltraTemp cooling
}

# -----------------------------
# Preset name -> Body.MODE map
# -----------------------------
PRESET_TO_MODE: Dict[str, int] = {
    "Gas Heater": 2,
    "Solar": 3,
    "Solar Preferred": 4,
    "UltraTemp Only": 5,
    "UltraTemp Preferred": 6,
    "Hybrid Gas Only": 7,
    "Hybrid Heat Pump Only": 8,
    "Hybrid Hybrid Mode": 9,
    "Hybrid Dual Mode": 10,
    "MasterTemp": 11,
    "Max-E-Therm": 12,
    "ETI250": 13,
    "Heat Pump Only": 14,
    "Heat Pump Preferred": 15,
}

# UltraTemp modes that should expose a temperature range (low/high)
COOLING_AVAILABLE_MODE_VALUES = {5, 6}

async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, async_add_entities):
    _LOGGER.info("intellicenter.climate: setup_entry starting")
    controller = hass.data[DOMAIN][entry.entry_id].controller

    bodies = controller.model.getByType(BODY_TYPE)
    
    
    # here we try to figure out which heater, if any, can be used for a given
    # body of water

    # first find all heaters
    # and sort them by their UI order (if they don't have one, use 100 and place them last)
    heaters = sorted(
        controller.model.getByType(HEATER_TYPE),
        key=lambda h: int(h[LISTORD_ATTR]) if h[LISTORD_ATTR] else 100,
    )
    _LOGGER.info("intellicenter.climate: found %d bodies and %d heaters", len(bodies), len(heaters))

    body_to_heaters: Dict[str, List[str]] = {}
    for body in bodies:
        body_to_heaters[body.objnam] = []
        _LOGGER.debug("intellicenter.climate: setup - body %s ", body.objnam)
    for heater in heaters:
        try:
            for bodynam in (heater[HEATER_BODY_FIELD] or "").split(" "):
                if bodynam in body_to_heaters:
                    body_to_heaters[bodynam].append(heater.objnam)
                    _LOGGER.debug("intellicenter.climate: setup - heater %s added to body %s", heater.objnam, bodynam)
        except Exception as ex:
            _LOGGER.warning("intellicenter.climate: error mapping heater %s: %s", heater.objnam, ex)

    entities: List[PoolClimate] = []
    for body in bodies:
        hlist = body_to_heaters.get(body.objnam, [])
        if not hlist:
            _LOGGER.info("intellicenter.climate: skipping body %s - no heaters", getattr(body, "objnam", None))
            continue
        _LOGGER.info("intellicenter.climate: creating entity for body %s with heaters=%s", getattr(body, "objnam", None), hlist)
        entities.append(PoolClimate(entry, controller, body, hlist))

    if entities:
        async_add_entities(sorted(entities, key=lambda e: int(e._poolObject[LISTORD_ATTR] or 100)))
        _LOGGER.info("intellicenter.climate: added %d entities", len(entities))
    else:
        _LOGGER.warning("intellicenter.climate: no entities created")


class PoolClimate(PoolEntity, ClimateEntity, RestoreEntity):
    """Climate entity for a BODY (Pool/Spa) with OFF/AUTO and preset strategies."""

    # We compute supported_features dynamically (see property below)

    def __init__(self, entry, controller, poolObject, heater_list: List[str]):
        super().__init__(entry, controller, poolObject, attribute_key=STATUS_ATTR)
        self._heater_list = heater_list
        self._attr_name = f"{self._poolObject[SNAME_ATTR]} Climate"
        self._available_presets: List[str] = self._compute_available_presets() # computed per update but we should bootstrap
        self._last_preset_name: Optional[str] = None
        _LOGGER.debug("intellicenter.climate: init entity for body=%s with heaters=%s as presets=%s", getattr(self._poolObject, "objnam", None), heater_list, self._available_presets)
        
        for h in heater_list:
            ho = controller.model[h]
            _LOGGER.debug("intellicenter.climate: Heater %s has subtype %s and name %s", getattr(ho, "objnam", None), getattr(ho, "subtype", None), getattr(ho, "sname", None))
            
    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        last = await self.async_get_last_state()
        if last:
            self._last_preset_name = last.attributes.get("last_preset_name")

    # ---------------
    # Entity identity
    # ---------------
    @property
    def unique_id(self) -> str:
        return super().unique_id + "_climate"

    # ---------------
    # Temperature IO
    # ---------------
    @property
    def temperature_unit(self):
        return self.pentairTemperatureSettings()

    @property
    def min_temp(self) -> float:
        return 5.0 if self._controller.systemInfo.usesMetric else 40.0

    @property
    def max_temp(self) -> float:
        # Guard rails: typical Pentair caps
        return 40.0 if self._controller.systemInfo.usesMetric else 104.0

    # Single setpoint (heat-only strategies)
    @property
    def target_temperature(self) -> Optional[float]:
        if self._cooling_available():
            return None
        try:
            return float(self._poolObject[LOTMP_ATTR])
        except Exception:
            return None

    # Range setpoints (Cooling Availa)
    @property
    def target_temperature_low(self) -> Optional[float]:
        if not self._cooling_available():
            return None
        try:
            return float(self._poolObject[LOTMP_ATTR])
        except Exception:
            return None

    @property
    def target_temperature_high(self) -> Optional[float]:
        if not self._cooling_available():
            return None
        try:
            return float(self._poolObject[HITMP_ATTR])
        except Exception:
            return None

    # Intellicenter increments in whole degrees, so we use 1.0
    @property
    def precision(self) -> float:
        return 1.0
    
    @property
    def target_temp_step(self) -> float:
        return 1.0    

    async def async_set_temperature(self, **kwargs: Any) -> None:
        if self._cooling_available():
            _LOGGER.debug("intellicenter.climate: async_set_temperature setting temp range with kwargs=%s", kwargs)
            # Expect range writes when cooling modes are selected
            low = kwargs.get("target_temp_low")
            high = kwargs.get("target_temp_high")
            changes: Dict[str, str] = {}
            if low is not None:
                changes[LOTMP_ATTR] = str(int(low))
                _LOGGER.debug("intellicenter.climate: setting low target temperature to %s", str(int(low)))
            if high is not None:
                changes[HITMP_ATTR] = str(int(high))
                _LOGGER.debug("intellicenter.climate: setting high target temperature to %s", str(int(high)))
            if changes:
                self.requestChanges(changes)
            return

        temp = kwargs.get(ATTR_TEMPERATURE)
        if temp is None:
            return
        self.requestChanges({LOTMP_ATTR: str(int(temp))})

    async def async_set_temperature_range(self, **kwargs: Any) -> None:
        _LOGGER.debug("intellicenter.climate: setting temp range with kwargs=%s", kwargs)

        if not self._cooling_available():
            return
        low = kwargs.get("target_temp_low")
        high = kwargs.get("target_temp_high")
        changes: Dict[str, str] = {}
        if low is not None:
            changes[LOTMP_ATTR] = str(int(low))
            _LOGGER.debug("intellicenter.climate: setting low target temperature to %s", str(int(low)))
        if high is not None:
            changes[HITMP_ATTR] = str(int(high))
            _LOGGER.debug("intellicenter.climate: setting high target temperature to %s", str(int(high)))
        if changes:
            self.requestChanges(changes)

    # -------------
    # HVAC control
    # -------------
    @property
    def hvac_modes(self) -> List[HVACMode]:
        # OFF / AUTO only
        return [HVACMode.OFF, HVACMode.AUTO]

    @property
    def hvac_mode(self) -> HVACMode:
        mode = int(_po_val(self._poolObject, MODE_ATTR, 1))
        return HVACMode.OFF if mode == 1 else HVACMode.AUTO

    async def async_set_hvac_mode(self, hvac_mode: HVACMode) -> None:
        if hvac_mode == HVACMode.OFF:
            self.requestChanges({MODE_ATTR: "1", HEATER_ATTR: NULL_OBJNAM})
            return
        # AUTO: restore last preset or pick a sensible default
        preset = self._last_preset_name
        if not preset or preset not in self._compute_available_presets():
            # Try preferred HP/UT, else gas, else first available
            for candidate in ("Heat Pump Preferred", "UltraTemp Preferred", "Gas Heater", "Solar", "Solar Preferred"):
                if candidate in self._compute_available_presets():
                    preset = candidate
                    break
            else:
                avail = self._compute_available_presets()
                preset = avail[0] if avail else None
        if preset:
            await self.async_set_preset_mode(preset)

    @property
    def current_temperature(self):
        """Return the current temperature."""
        return float(self._poolObject[LSTTMP_ATTR])


    @property
    def hvac_action(self) -> HVACAction:
        # Explicit OFF if body is OFF or MODE==1
        if _po_val(self._poolObject, STATUS_ATTR) == "OFF" or int(_po_val(self._poolObject, MODE_ATTR, 1)) == 1:
            return HVACAction.OFF
        try:
            code = int(_po_val(self._poolObject, HTMODE_ATTR, 0))
        except Exception:  # noqa: BLE001
            code = 0
        return HTMODE_TO_ACTION.get(code, HVACAction.IDLE)

    # --------------
    # Preset modes
    # --------------
    @property
    def preset_modes(self) -> List[str]:
        return self._compute_available_presets()

    def _compute_available_presets(self) -> List[str]:
        """Compute presets based directly on attached heaters."""
        allowed: List[str] = []

        for hname in self._heater_list:
            ho = self._controller.model[hname]
            sub = str(getattr(ho, "subtype", None)).upper()
            sname = _po_val(ho, "SNAME", hname)
            cool = _po_val(ho, "COOL", None)

            _LOGGER.debug(
                "heater %s: SUBTYP=%s SNAME=%s dir=%s",
                hname, sub, sname, dir(ho)
            )

            # Generic / model-specific gas
            if sub in ("GENERIC", "GAS") and "Gas Heater" in PRESET_TO_MODE:
                allowed.append("Gas Heater")
            if sub == "MASTER" and "MasterTemp" in PRESET_TO_MODE:
                allowed.append("MasterTemp")
            if sub == "MAX" and "Max-E-Therm" in PRESET_TO_MODE:
                allowed.append("Max-E-Therm")
            if sub == "ETI250" and "ETI250" in PRESET_TO_MODE:
                allowed.append("ETI250")

            # Heat pump
            if sub in ("HTPMP", "HEATPUMP", "HEAT_PUMP"):
                allowed.extend([m for m in ("Heat Pump Only", "Heat Pump Preferred") if m in PRESET_TO_MODE])

            # UltraTemp (cool-capable if COOL == ON or subtype == ULTRA)
            if sub == "ULTRA" or _po_val(ho, HEATER_COOL_FIELD) == "ON":
                allowed.extend([m for m in ("UltraTemp Only", "UltraTemp Preferred") if m in PRESET_TO_MODE])

            # Solar
            if sub == "SOLAR":
                allowed.extend([m for m in ("Solar", "Solar Preferred") if m in PRESET_TO_MODE])

        # Deduplicate and remember
        self._available_presets = list(dict.fromkeys(allowed))
        _LOGGER.debug("intellicenter.climate: available presets for body %s are %s", getattr(self._poolObject, "objnam", None), self._available_presets)
        return self._available_presets

    @property
    def preset_mode(self) -> Optional[str]:
        cur = int(_po_val(self._poolObject, MODE_ATTR, 1))
        for name, val in PRESET_TO_MODE.items():
            if val == cur and name in self._compute_available_presets():
                return name
        return None

    async def async_set_preset_mode(self, preset_mode: str) -> None:
        if preset_mode not in PRESET_TO_MODE:
            return
        if preset_mode not in self._compute_available_presets():
            return
        val = PRESET_TO_MODE[preset_mode]
        self._last_preset_name = preset_mode
        self.requestChanges({MODE_ATTR: str(val)})

    # ----------------------
    # Dynamic feature flags
    # ----------------------
    @property
    def supported_features(self) -> int:
        base = ClimateEntityFeature.PRESET_MODE
        if self._cooling_available():
            return base | ClimateEntityFeature.TARGET_TEMPERATURE_RANGE
        return base | ClimateEntityFeature.TARGET_TEMPERATURE

    # -------
    # Update
    # -------
    def isUpdated(self, updates: Dict[str, Dict[str, str]]) -> bool:
        body_objnam = self._poolObject.objnam
        body_keys = {STATUS_ATTR, HTMODE_ATTR, MODE_ATTR, LOTMP_ATTR, LSTTMP_ATTR, HITMP_ATTR, HEATER_ATTR, HTSRC_FIELD}
        if body_objnam in updates and body_keys & updates[body_objnam].keys():
            return True
        # Also refresh if any of our heaters change capabilities
        for h in self._heater_list:
            if h in updates and HEATER_COOL_FIELD in updates[h]:
                return True
        return False

    # -------
    # Helpers
    # -------
    def _cooling_available(self) -> bool:
        try:
            return int(_po_val(self._poolObject, MODE_ATTR, 1)) in COOLING_AVAILABLE_MODE_VALUES
        except Exception:
            return False

    # --------------
    # Diagnostics
    # --------------
    @property
    def extra_state_attributes(self) -> Dict[str, Any]:
        try:
            htmode = int(_po_val(self._poolObject, HTMODE_ATTR, 0))
        except Exception:
            htmode = 0
        return {
            "Body Name": self._poolObject.objnam,
            "Mode Number": int(_po_val(self._poolObject, MODE_ATTR, 1)),
            "Mode": self.preset_mode,
            "Heater Mode": htmode,
            "Heater Source": _po_val(self._poolObject, HTSRC_FIELD, ""),
            "Last Preset Used": self._last_preset_name,
            "Cooling?": self._cooling_available(),
        }
