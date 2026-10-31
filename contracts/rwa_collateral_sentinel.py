# v0.2.16
# { "Depends": "py-genlayer:1jb45aa8ynh2a9c9xn3b7qqh8sm5q93hwfp7jqmwsfhh8jpz09h6" }
from genlayer import *
from dataclasses import dataclass
import json


@allow_storage
@dataclass
class AssetVaultProfile:
    vault_id: str
    asset_symbol: str
    issuer_entity: str
    reserve_mandate: str
    base_ltv_bps: bigint
    current_ltv_bps: bigint
    base_liq_penalty_bps: bigint
    current_liq_penalty_bps: bigint
    max_borrow_cap: bigint
    active_borrow_cap: bigint
    circuit_state: str
    consecutive_warnings: bigint
    epoch_cursor: bigint
    new_borrows_allowed: bool


@allow_storage
@dataclass
class AttestationEpoch:
    snapshot_key: str
    vault_id: str
    epoch_number: bigint
    attestation_url: str
    custodian_feed_url: str
    reserve_coverage_tier: str
    liquidity_duration_tier: str
    custodian_standing: str
    resulting_ltv_bps: bigint
    resulting_cap: bigint
    resulting_state: str
    auditor_digest: str


@allow_storage
@dataclass
class ContagionIncident:
    incident_id: str
    vault_id: str
    bulletin_url: str
    threat_category: str
    confirmed_threat: bool
    recapitalization_url: str
    resolved: bool
    incident_notes: str


def derive_epoch_risk_curve(
    base_ltv: int,
    base_penalty: int,
    max_cap: int,
    prior_warnings: int,
    prior_state: str,
    coverage_tier: str,
    duration_tier: str,
    custodian_standing: str,
) -> tuple:
    # Severe solvency or custodian default immediately freezes vault
    if (
        coverage_tier == "UNDERCOLLATERALIZED_DEFICIT"
        or duration_tier == "ILLIQUID_IMPAIRED"
        or custodian_standing == "REGULATORY_FREEZE_OR_DEFAULT"
    ):
        return (
            0,
            min(2500, base_penalty + 1000),
            0,
            "EMERGENCY_FROZEN",
            prior_warnings + 1,
            False,
        )

    # Check warning conditions (narrow reserve buffer, maturity extension, or qualified audit)
    has_warning = (
        coverage_tier == "NARROW_BUFFER_98_TO_100"
        or duration_tier == "EXTENDED_MATURITY_LOCK"
        or custodian_standing == "QUALIFIED_CONCERN"
    )

    if has_warning:
        new_warnings = prior_warnings + 1
        # Multi-epoch hysteresis: 2+ consecutive warning epochs auto-escalate to BORROW_PAUSED
        if new_warnings >= 2:
            throttled_ltv = (base_ltv * 50) // 100
            return (
                throttled_ltv,
                min(2000, base_penalty + 500),
                0,
                "BORROW_PAUSED",
                new_warnings,
                False,
            )
        else:
            # First warning applies deterministic 25% LTV haircut and 50% borrow cap throttle
            throttled_ltv = (base_ltv * 75) // 100
            throttled_cap = (max_cap * 50) // 100
            return (
                throttled_ltv,
                min(2000, base_penalty + 250),
                throttled_cap,
                "THROTTLED_RISK",
                new_warnings,
                True,
            )

    # Clean attestation across all 3 risk dimensions:
    # If recovering from PROBATION_RECOVERY, graduate back to full NORMAL_OPERATION
    if prior_state == "PROBATION_RECOVERY":
        return (base_ltv, base_penalty, max_cap, "NORMAL_OPERATION", 0, True)

    return (base_ltv, base_penalty, max_cap, "NORMAL_OPERATION", 0, True)


class Contract(gl.Contract):
    vaults: TreeMap[str, AssetVaultProfile]
    epoch_snapshots: TreeMap[str, AttestationEpoch]
    incidents: TreeMap[str, ContagionIncident]
    vault_latest_incident: TreeMap[str, str]
    registered_vault_count: bigint
    total_epochs_synced: bigint
    total_incidents_logged: bigint
    active_frozen_vaults: bigint

    def __init__(self):
        self.registered_vault_count = bigint(0)
        self.total_epochs_synced = bigint(0)
        self.total_incidents_logged = bigint(0)
        self.active_frozen_vaults = bigint(0)

    @gl.public.write
    def onboard_collateral_vault(
        self,
        vault_id: str,
        asset_symbol: str,
        issuer_entity: str,
        reserve_mandate: str,
        base_ltv_bps: int,
        base_liq_penalty_bps: int,
        max_borrow_cap: int,
    ) -> str:
        if not vault_id or not isinstance(vault_id, str) or len(vault_id.strip()) == 0:
            raise gl.vm.UserError("Invalid vault_id: must be a non-empty string")
        clean_id = vault_id.strip().upper()

        if clean_id in self.vaults:
            raise gl.vm.UserError(f"Vault already onboarded: {clean_id}")

        if (
            not asset_symbol
            or not isinstance(asset_symbol, str)
            or len(asset_symbol.strip()) == 0
        ):
            raise gl.vm.UserError("Invalid asset_symbol: cannot be empty")

        if (
            not issuer_entity
            or not isinstance(issuer_entity, str)
            or len(issuer_entity.strip()) == 0
        ):
            raise gl.vm.UserError("Invalid issuer_entity: cannot be empty")

        if (
            not reserve_mandate
            or not isinstance(reserve_mandate, str)
            or len(reserve_mandate.strip()) < 10
        ):
            raise gl.vm.UserError(
                "Invalid reserve_mandate: provide clear backing rules (min 10 chars)"
            )

        if (
            not isinstance(base_ltv_bps, int)
            or base_ltv_bps < 1000
            or base_ltv_bps > 9500
        ):
            raise gl.vm.UserError(
                "Invalid base_ltv_bps: must be between 1000 (10%) and 9500 (95%)"
            )

        if (
            not isinstance(base_liq_penalty_bps, int)
            or base_liq_penalty_bps < 100
            or base_liq_penalty_bps > 2000
        ):
            raise gl.vm.UserError(
                "Invalid base_liq_penalty_bps: must be between 100 and 2000 bps"
            )

        if not isinstance(max_borrow_cap, int) or max_borrow_cap <= 0:
            raise gl.vm.UserError("Invalid max_borrow_cap: must be a positive integer")

        self.vaults[clean_id] = AssetVaultProfile(
            vault_id=clean_id,
            asset_symbol=asset_symbol.strip().upper(),
            issuer_entity=issuer_entity.strip(),
            reserve_mandate=reserve_mandate.strip(),
            base_ltv_bps=bigint(base_ltv_bps),
            current_ltv_bps=bigint(base_ltv_bps),
            base_liq_penalty_bps=bigint(base_liq_penalty_bps),
            current_liq_penalty_bps=bigint(base_liq_penalty_bps),
            max_borrow_cap=bigint(max_borrow_cap),
            active_borrow_cap=bigint(max_borrow_cap),
            circuit_state="NORMAL_OPERATION",
            consecutive_warnings=bigint(0),
            epoch_cursor=bigint(0),
            new_borrows_allowed=True,
        )

        self.registered_vault_count = bigint(int(self.registered_vault_count) + 1)
        return clean_id

    @gl.public.write
    def sync_epoch_solvency(
        self,
        vault_id: str,
        attestation_url: str,
        custodian_feed_url: str,
    ) -> str:
        if not vault_id or vault_id.strip().upper() not in self.vaults:
            raise gl.vm.UserError(f"Unknown vault_id: {vault_id}")
        clean_id = vault_id.strip().upper()

        vault = self.vaults[clean_id]
        if vault.circuit_state == "EMERGENCY_FROZEN":
            raise gl.vm.UserError(
                f"Vault {clean_id} is EMERGENCY_FROZEN; execute resolve_contagion_freeze before regular epoch sync"
            )

        for label, u in (
            ("attestation_url", attestation_url),
            ("custodian_feed_url", custodian_feed_url),
        ):
            if not u or not isinstance(u, str):
                raise gl.vm.UserError(f"Invalid {label}: must be a non-empty string")
            cu = u.strip()
            if not (cu.startswith("http://") or cu.startswith("https://")):
                raise gl.vm.UserError(
                    f"Invalid {label}: must start with http:// or https://"
                )

        clean_att_url = attestation_url.strip()
        clean_cust_url = custodian_feed_url.strip()

        # Pre-capture locals before nondet closures
        t_symbol = vault.asset_symbol
        t_issuer = vault.issuer_entity
        t_mandate = vault.reserve_mandate
        t_att_url = clean_att_url
        t_cust_url = clean_cust_url

        def leader_epoch_fn() -> dict:
            try:
                raw_att = gl.nondet.web.render(t_att_url, mode="text")
            except Exception as exc:
                raise gl.vm.UserError(
                    f"Failed to fetch reserve attestation URL: {str(exc)}"
                )
            if not raw_att or len(raw_att.strip()) == 0:
                raise gl.vm.UserError("Reserve attestation URL returned empty body")

            try:
                raw_cust = gl.nondet.web.render(t_cust_url, mode="text")
            except Exception as exc:
                raise gl.vm.UserError(f"Failed to fetch custodian feed URL: {str(exc)}")
            if not raw_cust or len(raw_cust.strip()) == 0:
                raise gl.vm.UserError("Custodian feed URL returned empty body")

            att_excerpt = raw_att.encode("ascii", errors="ignore").decode("ascii")[
                :4000
            ]
            cust_excerpt = raw_cust.encode("ascii", errors="ignore").decode("ascii")[
                :4000
            ]

            prompt = (
                "You are an institutional DeFi Risk & RWA Reserve Solvency Sentinel.\n\n"
                f"Collateral Asset: {t_symbol} (Issuer: {t_issuer})\n"
                f"Vault Reserve Mandate:\n{t_mandate}\n\n"
                f"Independent Auditor Reserve Attestation (first 4000 chars):\n{att_excerpt}\n\n"
                f"Custodian & Regulatory Telemetry Feed (first 4000 chars):\n{cust_excerpt}\n\n"
                "Classify the collateral health across three discrete risk dimensions.\n"
                "Return ONLY a JSON object with this exact schema:\n"
                "{\n"
                '  "reserve_coverage_tier": "OVERCOLLATERALIZED_100_PLUS" | "NARROW_BUFFER_98_TO_100" | "UNDERCOLLATERALIZED_DEFICIT",\n'
                '  "liquidity_duration_tier": "T_BILL_LIQUID" | "EXTENDED_MATURITY_LOCK" | "ILLIQUID_IMPAIRED",\n'
                '  "custodian_standing": "UNQUALIFIED_CLEAN" | "QUALIFIED_CONCERN" | "REGULATORY_FREEZE_OR_DEFAULT",\n'
                '  "auditor_digest": "<concise technical summary of reserve backing, maturity profile, and custodian status>"\n'
                "}\n\n"
                "Classification definitions:\n"
                "- OVERCOLLATERALIZED_100_PLUS: Verified reserves >= 100% of liabilities with clean surplus.\n"
                "- NARROW_BUFFER_98_TO_100: Minor mark-to-market dip (98%-99.9%) or delayed settlement in transit.\n"
                "- UNDERCOLLATERALIZED_DEFICIT: Verified reserves < 98% or unbacked minting detected.\n"
                "- T_BILL_LIQUID: Short-duration liquid T-bills/cash matching mandate.\n"
                "- EXTENDED_MATURITY_LOCK: Duration drift beyond mandate window without permanent impairment.\n"
                "- ILLIQUID_IMPAIRED: Distressed, gated, or defaulted underlying debt.\n"
                "- UNQUALIFIED_CLEAN: Unqualified audit sign-off and normal custodian redemptions.\n"
                "- QUALIFIED_CONCERN: Auditor emphasis-of-matter warning or delayed custodian reporting.\n"
                "- REGULATORY_FREEZE_OR_DEFAULT: Custodian receivership, regulatory seizure, or halted redemptions.\n"
                "Return valid JSON only."
            )

            try:
                raw_llm = gl.nondet.exec_prompt(prompt, response_format="json")
            except Exception as exc:
                raise gl.vm.UserError(f"LLM solvency evaluation failed: {str(exc)}")

            try:
                parsed = raw_llm if isinstance(raw_llm, dict) else json.loads(raw_llm)
            except Exception:
                raise gl.vm.UserError("Unparseable LLM JSON in solvency sync")

            if not isinstance(parsed, dict):
                raise gl.vm.UserError("LLM solvency output must be a JSON object")

            cov = str(parsed.get("reserve_coverage_tier", "")).strip().upper()
            dur = str(parsed.get("liquidity_duration_tier", "")).strip().upper()
            cus = str(parsed.get("custodian_standing", "")).strip().upper()

            if cov not in (
                "OVERCOLLATERALIZED_100_PLUS",
                "NARROW_BUFFER_98_TO_100",
                "UNDERCOLLATERALIZED_DEFICIT",
            ):
                raise gl.vm.UserError(f"Invalid reserve_coverage_tier: '{cov}'")

            if dur not in (
                "T_BILL_LIQUID",
                "EXTENDED_MATURITY_LOCK",
                "ILLIQUID_IMPAIRED",
            ):
                raise gl.vm.UserError(f"Invalid liquidity_duration_tier: '{dur}'")

            if cus not in (
                "UNQUALIFIED_CLEAN",
                "QUALIFIED_CONCERN",
                "REGULATORY_FREEZE_OR_DEFAULT",
            ):
                raise gl.vm.UserError(f"Invalid custodian_standing: '{cus}'")

            digest = (
                str(parsed.get("auditor_digest", "No digest"))
                .encode("ascii", errors="ignore")
                .decode("ascii")[:800]
            )

            return {
                "reserve_coverage_tier": cov,
                "liquidity_duration_tier": dur,
                "custodian_standing": cus,
                "auditor_digest": digest,
            }

        def validator_epoch_fn(leader_res) -> bool:
            if not isinstance(leader_res, gl.vm.Return):
                return False
            ld = leader_res.calldata
            if not isinstance(ld, dict):
                return False

            try:
                vd = leader_epoch_fn()
            except Exception:
                return False

            if not isinstance(vd, dict):
                return False

            # Strict 3-vector semantic equivalence: guarantees identical downstream LTV, penalty, and cap
            return (
                ld.get("reserve_coverage_tier") == vd.get("reserve_coverage_tier")
                and ld.get("liquidity_duration_tier")
                == vd.get("liquidity_duration_tier")
                and ld.get("custodian_standing") == vd.get("custodian_standing")
            )

        vec = gl.vm.run_nondet_unsafe(leader_epoch_fn, validator_epoch_fn)

        was_frozen = vault.circuit_state == "EMERGENCY_FROZEN"
        new_ltv, new_pen, new_cap, new_state, new_warn, allow_borrows = (
            derive_epoch_risk_curve(
                base_ltv=int(vault.base_ltv_bps),
                base_penalty=int(vault.base_liq_penalty_bps),
                max_cap=int(vault.max_borrow_cap),
                prior_warnings=int(vault.consecutive_warnings),
                prior_state=vault.circuit_state,
                coverage_tier=str(vec["reserve_coverage_tier"]),
                duration_tier=str(vec["liquidity_duration_tier"]),
                custodian_standing=str(vec["custodian_standing"]),
            )
        )

        next_epoch = int(vault.epoch_cursor) + 1
        snap_key = f"{clean_id}#E{next_epoch:04d}"

        vault.current_ltv_bps = bigint(new_ltv)
        vault.current_liq_penalty_bps = bigint(new_pen)
        vault.active_borrow_cap = bigint(new_cap)
        vault.circuit_state = new_state
        vault.consecutive_warnings = bigint(new_warn)
        vault.epoch_cursor = bigint(next_epoch)
        vault.new_borrows_allowed = bool(allow_borrows)
        self.vaults[clean_id] = vault

        if not was_frozen and new_state == "EMERGENCY_FROZEN":
            self.active_frozen_vaults = bigint(int(self.active_frozen_vaults) + 1)

        self.epoch_snapshots[snap_key] = AttestationEpoch(
            snapshot_key=snap_key,
            vault_id=clean_id,
            epoch_number=bigint(next_epoch),
            attestation_url=clean_att_url,
            custodian_feed_url=clean_cust_url,
            reserve_coverage_tier=str(vec["reserve_coverage_tier"]),
            liquidity_duration_tier=str(vec["liquidity_duration_tier"]),
            custodian_standing=str(vec["custodian_standing"]),
            resulting_ltv_bps=bigint(new_ltv),
            resulting_cap=bigint(new_cap),
            resulting_state=new_state,
            auditor_digest=str(vec["auditor_digest"]),
        )

        self.total_epochs_synced = bigint(int(self.total_epochs_synced) + 1)
        return snap_key

    @gl.public.write
    def trigger_contagion_breaker(
        self, vault_id: str, incident_bulletin_url: str
    ) -> str:
        if not vault_id or vault_id.strip().upper() not in self.vaults:
            raise gl.vm.UserError(f"Unknown vault_id: {vault_id}")
        clean_id = vault_id.strip().upper()

        if not incident_bulletin_url or not isinstance(incident_bulletin_url, str):
            raise gl.vm.UserError(
                "Invalid incident_bulletin_url: must be a non-empty string"
            )
        clean_bul_url = incident_bulletin_url.strip()
        if not (
            clean_bul_url.startswith("http://") or clean_bul_url.startswith("https://")
        ):
            raise gl.vm.UserError(
                "Invalid incident_bulletin_url: must start with http:// or https://"
            )

        vault = self.vaults[clean_id]
        t_symbol = vault.asset_symbol
        t_issuer = vault.issuer_entity
        t_bul_url = clean_bul_url

        def leader_breaker_fn() -> dict:
            try:
                raw_news = gl.nondet.web.render(t_bul_url, mode="text")
            except Exception as exc:
                raise gl.vm.UserError(
                    f"Failed to fetch incident bulletin URL: {str(exc)}"
                )
            if not raw_news or len(raw_news.strip()) == 0:
                raise gl.vm.UserError("Incident bulletin URL returned empty content")

            excerpt = raw_news.encode("ascii", errors="ignore").decode("ascii")[:4000]

            prompt = (
                "You are an Emergency DeFi Contagion Circuit-Breaker Sentinel.\n\n"
                f"Target Vault Asset: {t_symbol} (Issuer: {t_issuer})\n"
                f"Emergency Bulletin Evidence (first 4000 chars):\n{excerpt}\n\n"
                "Determine whether this bulletin confirms an acute solvency or custodian contagion event for this specific asset.\n"
                "Return ONLY a JSON object with this exact schema:\n"
                "{\n"
                '  "confirmed_threat": true | false,\n'
                '  "threat_category": "CUSTODIAN_INSOLVENCY" | "REDEMPTION_GATED" | "REGULATORY_SEIZURE" | "UNSUBSTANTIATED_NOISE",\n'
                '  "incident_notes": "<concise factual summary>"\n'
                "}"
            )

            try:
                llm_out = gl.nondet.exec_prompt(prompt, response_format="json")
            except Exception as exc:
                raise gl.vm.UserError(f"LLM contagion check error: {str(exc)}")

            try:
                parsed = llm_out if isinstance(llm_out, dict) else json.loads(llm_out)
            except Exception:
                raise gl.vm.UserError("Unparseable LLM contagion JSON")

            if not isinstance(parsed, dict):
                raise gl.vm.UserError("Invalid LLM contagion root type")

            confirmed = bool(parsed.get("confirmed_threat", False))
            cat = str(parsed.get("threat_category", "")).strip().upper()
            valid_cats = (
                "CUSTODIAN_INSOLVENCY",
                "REDEMPTION_GATED",
                "REGULATORY_SEIZURE",
                "UNSUBSTANTIATED_NOISE",
            )
            if cat not in valid_cats:
                raise gl.vm.UserError(f"Invalid threat_category: '{cat}'")

            if not confirmed:
                cat = "UNSUBSTANTIATED_NOISE"
            elif cat == "UNSUBSTANTIATED_NOISE":
                confirmed = False

            notes = (
                str(parsed.get("incident_notes", "No notes"))
                .encode("ascii", errors="ignore")
                .decode("ascii")[:600]
            )

            return {
                "confirmed_threat": confirmed,
                "threat_category": cat,
                "incident_notes": notes,
            }

        def validator_breaker_fn(leader_res) -> bool:
            if not isinstance(leader_res, gl.vm.Return):
                return False
            ld = leader_res.calldata
            if not isinstance(ld, dict):
                return False
            try:
                vd = leader_breaker_fn()
            except Exception:
                return False
            if not isinstance(vd, dict):
                return False

            return bool(ld.get("confirmed_threat")) == bool(
                vd.get("confirmed_threat")
            ) and ld.get("threat_category") == vd.get("threat_category")

        alert = gl.vm.run_nondet_unsafe(leader_breaker_fn, validator_breaker_fn)

        inc_idx = int(self.total_incidents_logged) + 1
        inc_id = f"INC-{inc_idx:06d}"
        is_confirmed = bool(alert["confirmed_threat"])

        self.incidents[inc_id] = ContagionIncident(
            incident_id=inc_id,
            vault_id=clean_id,
            bulletin_url=clean_bul_url,
            threat_category=str(alert["threat_category"]),
            confirmed_threat=is_confirmed,
            recapitalization_url="",
            resolved=not is_confirmed,
            incident_notes=str(alert["incident_notes"]),
        )

        self.total_incidents_logged = bigint(inc_idx)
        self.vault_latest_incident[clean_id] = inc_id

        if is_confirmed:
            if vault.circuit_state != "EMERGENCY_FROZEN":
                self.active_frozen_vaults = bigint(int(self.active_frozen_vaults) + 1)
            vault.circuit_state = "EMERGENCY_FROZEN"
            vault.current_ltv_bps = bigint(0)
            vault.current_liq_penalty_bps = bigint(
                min(2500, int(vault.base_liq_penalty_bps) + 1000)
            )
            vault.active_borrow_cap = bigint(0)
            vault.new_borrows_allowed = False
            self.vaults[clean_id] = vault

        return inc_id

    @gl.public.write
    def resolve_contagion_freeze(
        self, incident_id: str, recapitalization_proof_url: str
    ) -> str:
        if not incident_id or incident_id not in self.incidents:
            raise gl.vm.UserError(f"Unknown incident_id: {incident_id}")

        inc = self.incidents[incident_id]
        if inc.resolved:
            raise gl.vm.UserError(f"Incident {incident_id} is already resolved")

        if not recapitalization_proof_url or not isinstance(
            recapitalization_proof_url, str
        ):
            raise gl.vm.UserError(
                "Invalid recapitalization_proof_url: must be a non-empty string"
            )
        clean_recap_url = recapitalization_proof_url.strip()
        if not (
            clean_recap_url.startswith("http://")
            or clean_recap_url.startswith("https://")
        ):
            raise gl.vm.UserError(
                "Invalid recapitalization_proof_url: must start with http:// or https://"
            )

        vault = self.vaults[inc.vault_id]
        if vault.circuit_state != "EMERGENCY_FROZEN":
            raise gl.vm.UserError(
                f"Vault {inc.vault_id} is not in EMERGENCY_FROZEN state"
            )

        t_symbol = vault.asset_symbol
        t_mandate = vault.reserve_mandate
        t_threat = inc.threat_category
        t_recap_url = clean_recap_url

        def leader_unfreeze_fn() -> dict:
            try:
                raw_proof = gl.nondet.web.render(t_recap_url, mode="text")
            except Exception as exc:
                raise gl.vm.UserError(
                    f"Failed to fetch recapitalization proof URL: {str(exc)}"
                )
            if not raw_proof or len(raw_proof.strip()) == 0:
                raise gl.vm.UserError(
                    "Recapitalization proof URL returned empty content"
                )

            excerpt = raw_proof.encode("ascii", errors="ignore").decode("ascii")[:4000]

            prompt = (
                "You are an Institutional DeFi Post-Contagion Recovery Auditor.\n\n"
                f"Frozen Vault Asset: {t_symbol}\n"
                f"Prior Contagion Trigger: {t_threat}\n"
                f"Vault Reserve Mandate:\n{t_mandate}\n\n"
                f"Submitted Recapitalization / Custodian Cure Attestation (first 4000 chars):\n{excerpt}\n\n"
                "Verify whether reserves are fully restored >= 100% and custodian redemptions have resumed.\n"
                "Return ONLY a JSON object with this exact schema:\n"
                "{\n"
                '  "cure_verified": true | false,\n'
                '  "resolution_summary": "<concise verification explanation>"\n'
                "}"
            )

            try:
                llm_out = gl.nondet.exec_prompt(prompt, response_format="json")
            except Exception as exc:
                raise gl.vm.UserError(f"LLM recovery audit error: {str(exc)}")

            try:
                parsed = llm_out if isinstance(llm_out, dict) else json.loads(llm_out)
            except Exception:
                raise gl.vm.UserError("Unparseable LLM recovery JSON")

            if not isinstance(parsed, dict):
                raise gl.vm.UserError("Invalid LLM recovery root type")

            cured = bool(parsed.get("cure_verified", False))
            summary = (
                str(parsed.get("resolution_summary", "No summary"))
                .encode("ascii", errors="ignore")
                .decode("ascii")[:600]
            )

            return {"cure_verified": cured, "resolution_summary": summary}

        def validator_unfreeze_fn(leader_res) -> bool:
            if not isinstance(leader_res, gl.vm.Return):
                return False
            ld = leader_res.calldata
            if not isinstance(ld, dict):
                return False
            try:
                vd = leader_unfreeze_fn()
            except Exception:
                return False
            if not isinstance(vd, dict):
                return False

            return bool(ld.get("cure_verified")) == bool(vd.get("cure_verified"))

        rec_res = gl.vm.run_nondet_unsafe(leader_unfreeze_fn, validator_unfreeze_fn)

        inc.recapitalization_url = clean_recap_url
        inc.incident_notes = str(rec_res["resolution_summary"])

        if bool(rec_res["cure_verified"]):
            inc.resolved = True
            # Gradual probation unfreeze: restore LTV to 60% of baseline and cap to 25% until next clean epoch
            probation_ltv = (int(vault.base_ltv_bps) * 60) // 100
            probation_cap = (int(vault.max_borrow_cap) * 25) // 100
            vault.circuit_state = "PROBATION_RECOVERY"
            vault.current_ltv_bps = bigint(probation_ltv)
            vault.current_liq_penalty_bps = bigint(
                min(2000, int(vault.base_liq_penalty_bps) + 200)
            )
            vault.active_borrow_cap = bigint(probation_cap)
            vault.consecutive_warnings = bigint(0)
            vault.new_borrows_allowed = True
            self.vaults[inc.vault_id] = vault

            if int(self.active_frozen_vaults) > 0:
                self.active_frozen_vaults = bigint(int(self.active_frozen_vaults) - 1)

        self.incidents[incident_id] = inc
        return vault.circuit_state

    @gl.public.view
    def compute_max_borrowable(self, vault_id: str, collateral_units: int) -> int:
        clean_id = vault_id.strip().upper()
        if clean_id not in self.vaults:
            raise gl.vm.UserError(f"Unknown vault_id: {vault_id}")
        if not isinstance(collateral_units, int) or collateral_units < 0:
            raise gl.vm.UserError("collateral_units must be a non-negative integer")

        vault = self.vaults[clean_id]
        if not vault.new_borrows_allowed or int(vault.current_ltv_bps) == 0:
            return 0

        raw_limit = (collateral_units * int(vault.current_ltv_bps)) // 10000
        cap = int(vault.active_borrow_cap)
        return raw_limit if raw_limit < cap else cap

    @gl.public.view
    def get_vault_risk_schedule(self, vault_id: str) -> str:
        clean_id = vault_id.strip().upper()
        if clean_id not in self.vaults:
            raise gl.vm.UserError(f"Unknown vault_id: {vault_id}")
        v = self.vaults[clean_id]
        return json.dumps(
            {
                "vault_id": v.vault_id,
                "asset_symbol": v.asset_symbol,
                "issuer_entity": v.issuer_entity,
                "circuit_state": v.circuit_state,
                "base_ltv_bps": int(v.base_ltv_bps),
                "current_ltv_bps": int(v.current_ltv_bps),
                "base_liq_penalty_bps": int(v.base_liq_penalty_bps),
                "current_liq_penalty_bps": int(v.current_liq_penalty_bps),
                "max_borrow_cap": int(v.max_borrow_cap),
                "active_borrow_cap": int(v.active_borrow_cap),
                "consecutive_warnings": int(v.consecutive_warnings),
                "epoch_cursor": int(v.epoch_cursor),
                "new_borrows_allowed": bool(v.new_borrows_allowed),
            }
        )

    @gl.public.view
    def get_epoch_snapshot(self, snapshot_key: str) -> str:
        if snapshot_key not in self.epoch_snapshots:
            raise gl.vm.UserError(f"Epoch snapshot not found: {snapshot_key}")
        s = self.epoch_snapshots[snapshot_key]
        return json.dumps(
            {
                "snapshot_key": s.snapshot_key,
                "vault_id": s.vault_id,
                "epoch_number": int(s.epoch_number),
                "attestation_url": s.attestation_url,
                "custodian_feed_url": s.custodian_feed_url,
                "reserve_coverage_tier": s.reserve_coverage_tier,
                "liquidity_duration_tier": s.liquidity_duration_tier,
                "custodian_standing": s.custodian_standing,
                "resulting_ltv_bps": int(s.resulting_ltv_bps),
                "resulting_cap": int(s.resulting_cap),
                "resulting_state": s.resulting_state,
                "auditor_digest": s.auditor_digest,
            }
        )

    @gl.public.view
    def get_incident(self, incident_id: str) -> str:
        if incident_id not in self.incidents:
            raise gl.vm.UserError(f"Incident not found: {incident_id}")
        i = self.incidents[incident_id]
        return json.dumps(
            {
                "incident_id": i.incident_id,
                "vault_id": i.vault_id,
                "bulletin_url": i.bulletin_url,
                "threat_category": i.threat_category,
                "confirmed_threat": bool(i.confirmed_threat),
                "recapitalization_url": i.recapitalization_url,
                "resolved": bool(i.resolved),
                "incident_notes": i.incident_notes,
            }
        )

    @gl.public.view
    def get_sentinel_telemetry(self) -> str:
        return json.dumps(
            {
                "registered_vault_count": int(self.registered_vault_count),
                "total_epochs_synced": int(self.total_epochs_synced),
                "total_incidents_logged": int(self.total_incidents_logged),
                "active_frozen_vaults": int(self.active_frozen_vaults),
            }
        )
