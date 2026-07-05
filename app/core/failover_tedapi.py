import logging
import time
from typing import Optional
import urllib3
import requests
from pypowerwall.tedapi import TEDAPI, uses_api_lock
from pypowerwall.tedapi.tedapi_v1r import TEDAPIv1r
from pypowerwall.api_lock import acquire_lock_with_backoff

logger = logging.getLogger(__name__)


class FailoverTEDAPI(TEDAPI):
    """
    Subclass of TEDAPI that adds write-path failover to a WiFi/fallback host
    using the same RSA key/signing credentials.
    """

    def __init__(self, *args, v1r_fallback_host: Optional[str] = None, **kwargs):
        self.v1r_fallback_host = v1r_fallback_host
        self._fallback_transport = None
        self._probe_transport = None
        self._cached_rsa_key_path = None
        self.last_fallback_success = 0.0
        self.fallback_failed = False
        self.fallback_recover_after = 0.0
        self.fallback_fail_count = 0
        super().__init__(*args, **kwargs)
        logger.info(
            f"Initialized FailoverTEDAPI for host {self.gw_ip} with fallback {v1r_fallback_host}"
        )

    def is_fallback_viable(self) -> bool:
        if not self.v1r_fallback_host:
            return False
        if self.fallback_failed:
            if time.time() >= self.fallback_recover_after:
                return True
            return False
        return True

    def _record_fallback_failure(self):
        self.fallback_fail_count = getattr(self, "fallback_fail_count", 0) + 1
        self.fallback_failed = True
        backoff = min(60 * (2 ** (self.fallback_fail_count - 1)), 300)
        self.fallback_recover_after = time.time() + backoff
        logger.warning(
            f"Write/Read failover: fallback host {self.v1r_fallback_host} failed "
            f"({self.fallback_fail_count} consecutive times) — cooling down for {backoff}s"
        )

    def connect(self) -> bool:
        """
        Connect with fallback support. If LAN (primary) has failed, bypass
        the primary connection attempt to avoid a long login timeout, and instead
        bootstrap the DIN via the fallback host.
        """
        if not self.v1r_fallback_host:
            return super().connect()

        if getattr(self, "lan_failed", False):
            logger.warning(
                f"v1r: connect() called while LAN is failed. "
                f"Attempting to bootstrap DIN via fallback host: {self.v1r_fallback_host}"
            )
            rsa_key_path = self._resolve_rsa_key_path()
            if rsa_key_path:
                try:
                    fallback_transport = self._get_fallback_transport(rsa_key_path)
                    if fallback_transport.login():
                        din = fallback_transport.get_din()
                        if din:
                            self.din = din
                            logger.info(
                                f"v1r: Successfully bootstrapped DIN via fallback host: {self.din}"
                            )
                            self.last_fallback_success = time.time()
                            self.fallback_failed = False
                            self.fallback_fail_count = 0
                            return True
                except Exception as e:
                    logger.error(f"v1r: Connect via fallback host failed: {e}")
                    self._record_fallback_failure()
            return False

        return super().connect()

    def _write_config(self, updates: dict) -> bool:
        if not self.v1r or not self.v1r_transport:
            logger.error("_write_config requires v1r transport")
            return False

        if not self.v1r_fallback_host:
            logger.debug("No v1r_fallback_host configured, using default write path")
            return super()._write_config(updates)

        if not self.din:
            if not self.connect():
                if self.v1r_fallback_host:
                    rsa_key_path = self._resolve_rsa_key_path()
                    if rsa_key_path:
                        try:
                            self.din = self._get_fallback_transport(rsa_key_path).get_din()
                            if self.din:
                                # Bootstrap primary connect just failed — flag LAN as
                                # failed immediately so subsequent calls skip straight
                                # to fallback instead of paying a 3s primary-probe
                                # timeout for up to 3 more cycles.
                                self.lan_failed = True
                                self.lan_fail_count = 3
                                self.lan_recover_after = time.time() + 60
                                logger.warning(
                                    "v1r: Bootstrap primary connect failed — "
                                    "switching immediately to fallback host"
                                )
                        except Exception as e:
                            logger.error(f"Bootstrap: fallback DIN fetch failed: {e}")
                if not self.din:
                    logger.error("Not connected - unable to write config")
                    return False

        # Resolve rsa_key_path dynamically from settings
        rsa_key_path = self._resolve_rsa_key_path()
        if not rsa_key_path:
            logger.error("rsa_key_path could not be resolved from configuration")
            return False

        # 1. Consult the read-side failure state first.
        # If the read side already detected that primary is down, skip straight to fallback.
        if self.lan_failed:
            logger.warning(
                f"Write failover: read-side LAN already failed. "
                f"Writing straight to fallback host: {self.v1r_fallback_host}"
            )
            try:
                fallback_transport = self._get_fallback_transport(rsa_key_path)
                success = fallback_transport.write_config_v1r(self.din, updates)
                if success:
                    logger.info("Write failover: successfully wrote config to fallback host")
                    # Invalidate config cache
                    self.pwcache.pop("config", None)
                    self.pwcachetime.pop("config", None)
                    self.last_fallback_success = time.time()
                    self.fallback_failed = False
                    self.fallback_fail_count = 0
                    return True
                else:
                    logger.error("Write failover: failed to write config to fallback host")
                    self._record_fallback_failure()
                    return False
            except Exception as e:
                logger.error(f"Write failover: exception during fallback write: {e}")
                self._record_fallback_failure()
                return False

        # 2. Attempt primary write first, using a short explicit timeout probe (3 seconds)
        logger.debug(f"Attempting write config to primary host: {self.v1r_transport.host}")
        try:
            probe_transport = self._get_probe_transport(rsa_key_path)
            success = probe_transport.write_config_v1r(self.din, updates)
            if success:
                logger.info("Write config to primary host succeeded")
                # Invalidate config cache
                self.pwcache.pop("config", None)
                self.pwcachetime.pop("config", None)
                return True
            logger.warning("Write config to primary host returned False; failing over")
        except Exception as e:
            logger.warning(f"Write config to primary host failed with error: {e}; failing over")

        # 3. Fail over to fallback host immediately
        logger.warning(f"Failing over write to fallback host: {self.v1r_fallback_host}")
        try:
            fallback_transport = self._get_fallback_transport(rsa_key_path)
            success = fallback_transport.write_config_v1r(self.din, updates)
            if success:
                logger.info(
                    "Write failover: successfully wrote config to fallback host after primary failed"
                )
                # Invalidate config cache
                self.pwcache.pop("config", None)
                self.pwcachetime.pop("config", None)
                self.last_fallback_success = time.time()
                self.fallback_failed = False
                self.fallback_fail_count = 0
                return True
            else:
                logger.error(
                    "Write failover: failed to write config to fallback host after primary failed"
                )
                self._record_fallback_failure()
                return False
        except Exception as e:
            logger.error(
                f"Write failover: exception during fallback write after primary failed: {e}"
            )
            self._record_fallback_failure()
            return False

    def _resolve_rsa_key_path(self) -> Optional[str]:
        """Resolve rsa_key_path dynamically from settings. Cached after first resolution."""
        if getattr(self, "_cached_rsa_key_path", None):
            return self._cached_rsa_key_path
        rsa_key_path = None
        from app.config import settings
        clean_ip = self.gw_ip.split(':')[0] if self.gw_ip else None
        for gw in settings.gateways:
            if gw.host == clean_ip or gw.host == self.gw_ip:
                rsa_key_path = gw.rsa_key_path
                break
        if not rsa_key_path:
            rsa_key_path = settings.pw_rsa_key_path
        self._cached_rsa_key_path = rsa_key_path
        return rsa_key_path

    def _get_fallback_transport(self, rsa_key_path: str) -> TEDAPIv1r:
        """Lazily build and cache the fallback-host transport (reused across calls)."""
        if getattr(self, "_fallback_transport", None) is None:
            self._fallback_transport = TEDAPIv1r(
                host=self.v1r_fallback_host,
                password=self.v1r_transport.password,
                rsa_key_path=rsa_key_path,
                timeout=self.v1r_transport.timeout,
                poolmaxsize=self.v1r_transport.poolmaxsize,
            )
        return self._fallback_transport

    def _get_probe_transport(self, rsa_key_path: str) -> TEDAPIv1r:
        """Lazily build and cache the short-timeout, no-retry primary-probe transport."""
        if getattr(self, "_probe_transport", None) is None:
            probe = TEDAPIv1r(
                host=self.v1r_transport.host,
                password=self.v1r_transport.password,
                rsa_key_path=rsa_key_path,
                timeout=3,
                poolmaxsize=self.v1r_transport.poolmaxsize,
            )
            retries = urllib3.Retry(
                total=0, connect=0, read=0, redirect=0, status=0,
                raise_on_status=False,
            )
            adapter = requests.adapters.HTTPAdapter(
                max_retries=retries,
                pool_connections=probe.poolmaxsize,
                pool_maxsize=probe.poolmaxsize,
                pool_block=True,
            )
            probe.session.mount("https://", adapter)
            self._probe_transport = probe
        return self._probe_transport

    @uses_api_lock
    def get_config(self, self_function=None, force=False) -> Optional[dict]:
        """
        Get the Powerwall Gateway Configuration with failover support.
        """
        if not self.v1r or not self.v1r_transport or not self.v1r_fallback_host:
            return super().get_config(force=force)

        # 1. Check cache first
        if not force and "config" in self.pwcachetime:
            age = time.time() - self.pwcachetime["config"]
            if age < self.pwconfigexpire:
                logger.debug(f"Using Cached Config (age: {age:.2f}s, expire: {self.pwconfigexpire}s)")
                return self.pwcache["config"]

        if not force and self.pwcooldown > time.perf_counter():
            logger.debug('Rate limit cooldown period - Pausing API calls')
            return None

        # 2. Acquire lock for the API call
        with acquire_lock_with_backoff(self_function, self.timeout):
            # Double-check cache
            if not force and "config" in self.pwcachetime:
                if time.time() - self.pwcachetime["config"] < self.pwconfigexpire:
                    logger.debug("Using Cached Payload (double-check)")
                    return self.pwcache["config"]

            if not force and self.pwcooldown > time.perf_counter():
                logger.debug('Rate limit cooldown period - Pausing API calls')
                return None

            if not self.din:
                if not self.connect():
                    if self.v1r_fallback_host:
                        rsa_key_path = self._resolve_rsa_key_path()
                        if rsa_key_path:
                            try:
                                self.din = self._get_fallback_transport(rsa_key_path).get_din()
                                if self.din:
                                    # Bootstrap primary connect just failed — flag LAN as
                                    # failed immediately so subsequent calls skip straight
                                    # to fallback instead of paying a 3s primary-probe
                                    # timeout for up to 3 more cycles.
                                    self.lan_failed = True
                                    self.lan_fail_count = 3
                                    self.lan_recover_after = time.time() + 60
                                    logger.warning(
                                        "v1r: Bootstrap primary connect failed — "
                                        "switching immediately to fallback host"
                                    )
                            except Exception as e:
                                logger.error(f"Bootstrap: fallback DIN fetch failed: {e}")
                    if not self.din:
                        logger.error("Not Connected - Unable to get configuration")
                        return None

            rsa_key_path = self._resolve_rsa_key_path()
            if not rsa_key_path:
                logger.error("rsa_key_path could not be resolved from configuration")
                return None

            # Recovery probe: if LAN was marked failed but the backoff window has
            # passed, attempt a real reconnect before deciding whether to route
            # via fallback. self._connect_v1r() (base class) clears lan_failed /
            # lan_fail_count / lan_recover_after internally on success.
            if self.lan_failed and time.time() >= self.lan_recover_after:
                logger.info("v1r: LAN recovery window reached — attempting reconnect")
                if self._connect_v1r():
                    logger.info("v1r: LAN recovered — resuming primary path")
                else:
                    self.lan_fail_count += 1
                    backoff = min(60 * (2 ** self.lan_fail_count), 7680)
                    self.lan_recover_after = time.time() + backoff
                    logger.warning(
                        f"v1r: LAN still unreachable, next retry in {backoff:.0f}s"
                    )

            # 3. Consult the read-side failure state first.
            if self.lan_failed:
                logger.warning(
                    f"Get config failover: LAN already failed. "
                    f"Fetching straight from fallback host: {self.v1r_fallback_host}"
                )
                try:
                    fallback_transport = self._get_fallback_transport(rsa_key_path)
                    data = fallback_transport.get_config_v1r(self.din)
                    if data:
                        logger.info("Get config failover: successfully fetched config from fallback host")
                        self.pwcachetime["config"] = time.time()
                        self.pwcache["config"] = data
                        self.last_fallback_success = time.time()
                        self.fallback_failed = False
                        self.fallback_fail_count = 0
                        return data
                    else:
                        logger.error("Get config failover: failed to fetch config from fallback host")
                        self._record_fallback_failure()
                        return None
                except Exception as e:
                    logger.error(f"Get config failover: exception during fallback read: {e}")
                    self._record_fallback_failure()
                    return None

            # 4. Attempt primary read, using a short explicit timeout probe (3 seconds)
            logger.debug(f"Attempting get config from primary host: {self.v1r_transport.host}")
            try:
                probe_transport = self._get_probe_transport(rsa_key_path)
                data = probe_transport.get_config_v1r(self.din)
                if data:
                    logger.info("Get config from primary host succeeded")
                    self.pwcachetime["config"] = time.time()
                    self.pwcache["config"] = data
                    
                    # Reset failure tracking on success
                    self.lan_fail_count = 0
                    self.lan_last_success = time.time()
                    return data
                
                logger.warning("Get config from primary host returned empty/False; failing over")
            except Exception as e:
                logger.warning(f"Get config from primary host failed with error: {e}; failing over")

            # 5. LAN call failed — track for failover
            self.lan_fail_count += 1
            if self.lan_fail_count >= 3:
                self.lan_failed = True
                backoff = min(60 * (2 ** self.lan_fail_count), 7680)
                self.lan_recover_after = time.time() + backoff
                logger.warning(
                    f"v1r: LAN failed {self.lan_fail_count} consecutive times — "
                    f"switching to fallback host (retry LAN in {backoff:.0f}s)"
                )

            # 6. Fall back to fallback host immediately
            logger.warning(f"Failing over get config to fallback host: {self.v1r_fallback_host}")
            try:
                fallback_transport = self._get_fallback_transport(rsa_key_path)
                data = fallback_transport.get_config_v1r(self.din)
                if data:
                    logger.info("Get config failover: successfully fetched config from fallback host after primary failed")
                    self.pwcachetime["config"] = time.time()
                    self.pwcache["config"] = data
                    self.last_fallback_success = time.time()
                    self.fallback_failed = False
                    self.fallback_fail_count = 0
                    return data
                else:
                    logger.error("Get config failover: failed to fetch config from fallback host after primary failed")
                    self._record_fallback_failure()
                    return None
            except Exception as e:
                logger.error(f"Get config failover: exception during fallback read after primary failed: {e}")
                self._record_fallback_failure()
                return None

    def get_din(self, force=False) -> Optional[str]:
        """
        Get the Device Identification Number (DIN) with failover support.
        """
        if not self.v1r or not self.v1r_transport or not self.v1r_fallback_host:
            return super().get_din(force=force)

        # 1. Check cache first
        if not force and "din" in self.pwcachetime:
            if time.time() - self.pwcachetime["din"] < self.pwcacheexpire:
                logger.debug("Using Cached DIN")
                return self.pwcache["din"]

        if not force and self.pwcooldown > time.perf_counter():
            logger.debug('Rate limit cooldown period - Pausing API calls')
            return None

        rsa_key_path = self._resolve_rsa_key_path()
        if not rsa_key_path:
            logger.error("rsa_key_path could not be resolved from configuration")
            return None

        # Recovery probe: if LAN was marked failed but the backoff window has
        # passed, attempt a real reconnect before deciding whether to route
        # via fallback. self._connect_v1r() (base class) clears lan_failed /
        # lan_fail_count / lan_recover_after internally on success.
        if self.lan_failed and time.time() >= self.lan_recover_after:
            logger.info("v1r: LAN recovery window reached — attempting reconnect")
            if self._connect_v1r():
                logger.info("v1r: LAN recovered — resuming primary path")
                # _connect_v1r() already fetched the DIN as part of reconnecting —
                # return it directly instead of re-probing primary a second time.
                self.pwcachetime["din"] = time.time()
                self.pwcache["din"] = self.din
                return self.din
            else:
                self.lan_fail_count += 1
                backoff = min(60 * (2 ** self.lan_fail_count), 7680)
                self.lan_recover_after = time.time() + backoff
                logger.warning(
                    f"v1r: LAN still unreachable, next retry in {backoff:.0f}s"
                )

        # 2. Consult the read-side failure state first.
        if self.lan_failed:
            logger.warning(
                f"Get DIN failover: LAN already failed. "
                f"Fetching straight from fallback host: {self.v1r_fallback_host}"
            )
            try:
                fallback_transport = self._get_fallback_transport(rsa_key_path)
                din = fallback_transport.get_din()
                if din:
                    logger.info("Get DIN failover: successfully fetched DIN from fallback host")
                    self.pwcachetime["din"] = time.time()
                    self.pwcache["din"] = din
                    self.last_fallback_success = time.time()
                    self.fallback_failed = False
                    self.fallback_fail_count = 0
                    return din
                else:
                    logger.error("Get DIN failover: failed to fetch DIN from fallback host")
                    self._record_fallback_failure()
                    return None
            except Exception as e:
                logger.error(f"Get DIN failover: exception during fallback read: {e}")
                self._record_fallback_failure()
                return None

        # 3. Attempt primary read, using a short explicit timeout probe (3 seconds)
        logger.debug(f"Attempting get DIN from primary host: {self.v1r_transport.host}")
        try:
            probe_transport = self._get_probe_transport(rsa_key_path)
            din = probe_transport.get_din()
            if din:
                logger.info("Get DIN from primary host succeeded")
                self.pwcachetime["din"] = time.time()
                self.pwcache["din"] = din
                
                # Reset failure tracking on success
                self.lan_fail_count = 0
                self.lan_last_success = time.time()
                return din
            
            logger.warning("Get DIN from primary host returned empty/False; failing over")
        except Exception as e:
            logger.warning(f"Get DIN from primary host failed with error: {e}; failing over")

        # 4. LAN call failed — track for failover
        self.lan_fail_count += 1
        if self.lan_fail_count >= 3:
            self.lan_failed = True
            backoff = min(60 * (2 ** self.lan_fail_count), 7680)
            self.lan_recover_after = time.time() + backoff
            logger.warning(
                f"v1r: LAN failed {self.lan_fail_count} consecutive times — "
                f"switching to fallback host (retry LAN in {backoff:.0f}s)"
            )

        # 5. Fall back to fallback host immediately
        logger.warning(f"Failing over get DIN to fallback host: {self.v1r_fallback_host}")
        try:
            fallback_transport = self._get_fallback_transport(rsa_key_path)
            din = fallback_transport.get_din()
            if din:
                logger.info("Get DIN failover: successfully fetched DIN from fallback host after primary failed")
                self.pwcachetime["din"] = time.time()
                self.pwcache["din"] = din
                self.last_fallback_success = time.time()
                self.fallback_failed = False
                self.fallback_fail_count = 0
                return din
            else:
                logger.error("Get DIN failover: failed to fetch DIN from fallback host after primary failed")
                self._record_fallback_failure()
                return None
        except Exception as e:
            logger.error(f"Get DIN failover: exception during fallback read after primary failed: {e}")
            self._record_fallback_failure()
            return None


def wrap_gateway(pw, v1r_fallback_host: Optional[str]):
    """
    Wraps the internal TEDAPI instance of a pypowerwall.Powerwall connection
    object with FailoverTEDAPI to enable write failover.
    """
    if not v1r_fallback_host:
        return pw

    if hasattr(pw, "tedapi") and pw.tedapi:
        # Patch the tedapi instance dynamically to use FailoverTEDAPI subclass
        pw.tedapi.__class__ = FailoverTEDAPI
        pw.tedapi.v1r_fallback_host = v1r_fallback_host
        logger.info(
            f"Successfully patched tedapi for gateway with write-path failover to {v1r_fallback_host}"
        )
    else:
        logger.warning("wrap_gateway: connection object does not have a tedapi transport")
    return pw

