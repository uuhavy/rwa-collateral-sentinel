# Test suite for RWACollateralSolvencySentinel GenLayer Intelligent Contract
# Compatible with gltest, direct_vm, and standalone Python test environments.

import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import json
import pytest


class MockReturn:
    def __init__(self, calldata):
        self.calldata = calldata


class MockUserError(Exception):
    pass


class MockNondetWeb:
    def __init__(self):
        self.urls = {}

    def render(self, url: str, mode: str = "text") -> str:
        for pattern, content in self.urls.items():
            if pattern in url or url in pattern:
                if isinstance(content, dict):
                    if content.get("status", 200) >= 400:
                        raise Exception(f"HTTP Error {content.get('status')}")
                    return content.get("body", "")
                return str(content)
        raise Exception(f"Failed to fetch URL: {url} (404 Not Found)")


class MockNondet:
    def __init__(self):
        self.web = MockNondetWeb()
        self.prompts = {}
        self.call_count = 0

    def exec_prompt(self, prompt: str, response_format: str = "json") -> str:
        self.call_count += 1
        for pattern, response in self.prompts.items():
            if pattern in prompt or prompt in pattern:
                if isinstance(response, str):
                    return response
                return json.dumps(response)

        return json.dumps(
            {
                "reserve_coverage_tier": "OVERCOLLATERALIZED_100_PLUS",
                "liquidity_duration_tier": "T_BILL_LIQUID",
                "custodian_standing": "UNQUALIFIED_CLEAN",
                "auditor_digest": "Default clean attestation.",
            }
        )


class MockGenVM:
    UserError = MockUserError
    Return = MockReturn

    def __init__(self):
        self.validator_override = None

    def run_nondet_unsafe(self, leader_fn, validator_fn):
        leader_output = leader_fn()
        leader_res = MockReturn(leader_output)

        if self.validator_override is not None:
            is_valid = self.validator_override(leader_res)
        else:
            is_valid = validator_fn(leader_res)

        if not is_valid:
            raise Exception("Consensus failed: Validator rejected leader proposal")

        return leader_res.calldata


class MockPublic:
    @staticmethod
    def write(fn):
        return fn

    @staticmethod
    def view(fn):
        return fn


class GenericMockStorage(dict):
    def __class_getitem__(cls, item):
        return cls


class GenericMockArray(list):
    def __class_getitem__(cls, item):
        return cls


class MockGL:
    def __init__(self):
        self.nondet = MockNondet()
        self.vm = MockGenVM()
        self.public = MockPublic()

    class Contract:
        pass


import types

if "genlayer" not in sys.modules:
    _gl_mod = types.ModuleType("genlayer")
    _gl_inst = MockGL()
    _gl_mod.gl = _gl_inst
    _gl_mod.allow_storage = lambda cls: cls
    _gl_mod.bigint = int
    _gl_mod.TreeMap = GenericMockStorage
    _gl_mod.DynArray = GenericMockArray
    _gl_mod.Address = str
    sys.modules["genlayer"] = _gl_mod


def sim_installMocks(target_env, mocks: dict):
    web_mocks = mocks.get("nondet_web_request") or mocks.get("web") or {}
    llm_mocks = mocks.get("nondet_exec_prompt") or mocks.get("llm") or {}

    if hasattr(target_env, "nondet"):
        for url_pattern, content in web_mocks.items():
            target_env.nondet.web.urls[url_pattern] = content
        for prompt_pattern, response in llm_mocks.items():
            target_env.nondet.prompts[prompt_pattern] = response


@pytest.fixture
def test_env(monkeypatch):
    mock_gl = MockGL()
    import contracts.rwa_collateral_sentinel as rcs

    monkeypatch.setattr(rcs, "gl", mock_gl)
    monkeypatch.setattr(rcs.gl.vm, "UserError", MockUserError)

    contract = rcs.Contract()
    contract.vaults = {}
    contract.epoch_snapshots = {}
    contract.incidents = {}
    contract.vault_latest_incident = {}
    contract.registered_vault_count = 0
    contract.total_epochs_synced = 0
    contract.total_incidents_logged = 0
    contract.active_frozen_vaults = 0

    return {
        "contract": contract,
        "mock_gl": mock_gl,
        "module": rcs,
    }


def test_onboard_vault_and_clean_epoch_sync(test_env):
    """Onboard RWA vault and verify clean attestation preserves baseline LTV and cap."""
    contract = test_env["contract"]
    mock_gl = test_env["mock_gl"]

    vid = contract.onboard_collateral_vault(
        vault_id="ustb-vault-01",
        asset_symbol="USTB",
        issuer_entity="Superstate Short Duration Trust",
        reserve_mandate="100% US Treasury Bills maturing within 90 days held at qualified custodian.",
        base_ltv_bps=8000,
        base_liq_penalty_bps=400,
        max_borrow_cap=10_000_000,
    )
    assert vid == "USTB-VAULT-01"
    assert contract.compute_max_borrowable("USTB-VAULT-01", 1_000_000) == 800_000

    att_url = "https://attest.superstate.co/2026-10"
    cust_url = "https://custodian.umb.com/feeds/ustb"
    sim_installMocks(
        mock_gl,
        {
            "nondet_web_request": {
                att_url: "NAV 101.4% backed by 45-day T-bills.",
                cust_url: "Unqualified custodian sign-off, daily T+0 redemptions active.",
            },
            "nondet_exec_prompt": {
                "RWA Reserve Solvency Sentinel": {
                    "reserve_coverage_tier": "OVERCOLLATERALIZED_100_PLUS",
                    "liquidity_duration_tier": "T_BILL_LIQUID",
                    "custodian_standing": "UNQUALIFIED_CLEAN",
                    "auditor_digest": "101.4% T-bill backing verified.",
                }
            },
        },
    )

    snap_key = contract.sync_epoch_solvency("USTB-VAULT-01", att_url, cust_url)
    assert snap_key == "USTB-VAULT-01#E0001"

    sched = json.loads(contract.get_vault_risk_schedule("USTB-VAULT-01"))
    assert sched["circuit_state"] == "NORMAL_OPERATION"
    assert sched["current_ltv_bps"] == 8000
    assert sched["active_borrow_cap"] == 10_000_000


def test_multi_epoch_hysteresis_warning_escalation(test_env):
    """Two consecutive warning epochs automatically escalate from THROTTLED_RISK to BORROW_PAUSED."""
    contract = test_env["contract"]
    mock_gl = test_env["mock_gl"]

    contract.onboard_collateral_vault(
        "OUSG-VAULT",
        "OUSG",
        "Ondo Short-Term US Govt",
        "Short-term Treasury backing >= 100%.",
        base_ltv_bps=8000,
        base_liq_penalty_bps=500,
        max_borrow_cap=5_000_000,
    )

    mock_gl.nondet.web.urls["https://att.org/e1"] = "Buffer dipped to 99.1%."
    mock_gl.nondet.web.urls["https://cust.org/e1"] = "Normal custodian."
    mock_gl.nondet.prompts["RWA Reserve Solvency Sentinel"] = json.dumps(
        {
            "reserve_coverage_tier": "NARROW_BUFFER_98_TO_100",
            "liquidity_duration_tier": "T_BILL_LIQUID",
            "custodian_standing": "UNQUALIFIED_CLEAN",
            "auditor_digest": "99.1% transit dip.",
        }
    )

    # Epoch 1 -> THROTTLED_RISK (75% of 8000 = 6000 bps LTV, 50% cap = 2,500,000)
    contract.sync_epoch_solvency(
        "OUSG-VAULT", "https://att.org/e1", "https://cust.org/e1"
    )
    s1 = json.loads(contract.get_vault_risk_schedule("OUSG-VAULT"))
    assert s1["circuit_state"] == "THROTTLED_RISK"
    assert s1["current_ltv_bps"] == 6000
    assert s1["active_borrow_cap"] == 2_500_000
    assert s1["consecutive_warnings"] == 1

    # Epoch 2 consecutive warning -> escalates to BORROW_PAUSED (cap = 0, new_borrows_allowed = False)
    contract.sync_epoch_solvency(
        "OUSG-VAULT", "https://att.org/e1", "https://cust.org/e1"
    )
    s2 = json.loads(contract.get_vault_risk_schedule("OUSG-VAULT"))
    assert s2["circuit_state"] == "BORROW_PAUSED"
    assert s2["current_ltv_bps"] == 4000
    assert s2["active_borrow_cap"] == 0
    assert s2["new_borrows_allowed"] is False
    assert contract.compute_max_borrowable("OUSG-VAULT", 1_000_000) == 0


def test_emergency_contagion_breaker_and_probation_graduation(test_env):
    """Black-swan contagion freezes vault, recapitalization moves to PROBATION_RECOVERY, next clean epoch restores NORMAL_OPERATION."""
    contract = test_env["contract"]
    mock_gl = test_env["mock_gl"]

    contract.onboard_collateral_vault(
        "USDM-VAULT",
        "USDM",
        "Mountain Protocol",
        "Backed 100% by short-dated Treasury bills.",
        base_ltv_bps=7500,
        base_liq_penalty_bps=500,
        max_borrow_cap=8_000_000,
    )

    bul_url = "https://regulator.bm/notices/custodian-receivership"
    recap_url = "https://mountainprotocol.com/attestations/new-custodian-cure"
    clean_att = "https://mountainprotocol.com/attestations/monthly-clean"
    clean_cust = "https://bnymellon.com/feeds/usdm-active"

    sim_installMocks(
        mock_gl,
        {
            "nondet_web_request": {
                bul_url: "Primary sub-custodian placed under receivership; redemptions gated.",
                recap_url: "Reserves migrated 100% to BNY Mellon; redemptions fully reopened.",
                clean_att: "102% T-bill reserve backing verified.",
                clean_cust: "BNY Mellon custody active with unqualified standing.",
            },
            "nondet_exec_prompt": {
                "Contagion Circuit-Breaker Sentinel": {
                    "confirmed_threat": True,
                    "threat_category": "CUSTODIAN_INSOLVENCY",
                    "incident_notes": "Sub-custodian receivership confirmed.",
                },
                "Post-Contagion Recovery Auditor": {
                    "cure_verified": True,
                    "resolution_summary": "Reserves migrated to BNY Mellon at 102% coverage.",
                },
                "RWA Reserve Solvency Sentinel": {
                    "reserve_coverage_tier": "OVERCOLLATERALIZED_100_PLUS",
                    "liquidity_duration_tier": "T_BILL_LIQUID",
                    "custodian_standing": "UNQUALIFIED_CLEAN",
                    "auditor_digest": "Clean post-migration epoch.",
                },
            },
        },
    )

    # 1. Trip contagion breaker -> EMERGENCY_FROZEN
    inc_id = contract.trigger_contagion_breaker("USDM-VAULT", bul_url)
    assert inc_id == "INC-000001"
    frozen_sched = json.loads(contract.get_vault_risk_schedule("USDM-VAULT"))
    assert frozen_sched["circuit_state"] == "EMERGENCY_FROZEN"
    assert frozen_sched["current_ltv_bps"] == 0
    assert contract.compute_max_borrowable("USDM-VAULT", 500_000) == 0

    # Regular epoch sync is blocked while EMERGENCY_FROZEN
    UserError = mock_gl.vm.UserError
    with pytest.raises(UserError, match="EMERGENCY_FROZEN"):
        contract.sync_epoch_solvency("USDM-VAULT", clean_att, clean_cust)

    # 2. Resolve contagion freeze -> PROBATION_RECOVERY (60% of 7500 = 4500 bps LTV, 25% of 8M = 2M cap)
    post_cure_state = contract.resolve_contagion_freeze(inc_id, recap_url)
    assert post_cure_state == "PROBATION_RECOVERY"
    prob_sched = json.loads(contract.get_vault_risk_schedule("USDM-VAULT"))
    assert prob_sched["current_ltv_bps"] == 4500
    assert prob_sched["active_borrow_cap"] == 2_000_000

    # 3. Clean epoch sync graduates PROBATION_RECOVERY -> NORMAL_OPERATION (7500 bps LTV, 8M cap)
    contract.sync_epoch_solvency("USDM-VAULT", clean_att, clean_cust)
    grad_sched = json.loads(contract.get_vault_risk_schedule("USDM-VAULT"))
    assert grad_sched["circuit_state"] == "NORMAL_OPERATION"
    assert grad_sched["current_ltv_bps"] == 7500
    assert grad_sched["active_borrow_cap"] == 8_000_000


def test_unsubstantiated_rumor_does_not_freeze_vault(test_env):
    """False alarm bulletin logs resolved incident without tripping circuit breaker."""
    contract = test_env["contract"]
    mock_gl = test_env["mock_gl"]

    contract.onboard_collateral_vault(
        "BUIDL-VAULT",
        "BUIDL",
        "BlackRock USD Institutional Fund",
        "100% US Treasury bills and repo agreements.",
        8500,
        300,
        20_000_000,
    )

    rumor_url = "https://social-blog.example/fake-rumor"
    mock_gl.nondet.web.urls[rumor_url] = "Unverified anonymous rumor."
    mock_gl.nondet.prompts["Contagion Circuit-Breaker Sentinel"] = json.dumps(
        {
            "confirmed_threat": False,
            "threat_category": "UNSUBSTANTIATED_NOISE",
            "incident_notes": "Anonymous blog post without factual or regulatory evidence.",
        }
    )

    inc_id = contract.trigger_contagion_breaker("BUIDL-VAULT", rumor_url)
    inc = json.loads(contract.get_incident(inc_id))
    assert inc["confirmed_threat"] is False
    assert inc["resolved"] is True

    sched = json.loads(contract.get_vault_risk_schedule("BUIDL-VAULT"))
    assert sched["circuit_state"] == "NORMAL_OPERATION"
    assert sched["current_ltv_bps"] == 8500


def test_consensus_rejects_vector_divergence(test_env):
    """Validator rejects leader if any of the 3 risk vectors diverge, guaranteeing identical risk curve output."""
    contract = test_env["contract"]
    mock_gl = test_env["mock_gl"]

    contract.onboard_collateral_vault(
        "TBILL-VAULT",
        "TBILL",
        "OpenEden T-Bill",
        "Short-term Treasury backing >= 100%.",
        8000,
        400,
        5_000_000,
    )

    mock_gl.nondet.web.urls["https://a.org"] = "Attestation"
    mock_gl.nondet.web.urls["https://c.org"] = "Custodian"
    mock_gl.nondet.prompts["RWA Reserve Solvency Sentinel"] = json.dumps(
        {
            "reserve_coverage_tier": "OVERCOLLATERALIZED_100_PLUS",
            "liquidity_duration_tier": "T_BILL_LIQUID",
            "custodian_standing": "UNQUALIFIED_CLEAN",
            "auditor_digest": "Clean",
        }
    )

    def diverging_validator(leader_res):
        # Validator classifies duration as EXTENDED_MATURITY_LOCK instead of T_BILL_LIQUID
        return (
            leader_res.calldata["reserve_coverage_tier"]
            == "OVERCOLLATERALIZED_100_PLUS"
            and leader_res.calldata["liquidity_duration_tier"]
            == "EXTENDED_MATURITY_LOCK"
        )

    mock_gl.vm.validator_override = diverging_validator

    with pytest.raises(
        Exception, match="Consensus failed: Validator rejected leader proposal"
    ):
        contract.sync_epoch_solvency("TBILL-VAULT", "https://a.org", "https://c.org")


def test_input_and_duplicate_vault_errors(test_env):
    """Verify UserError boundaries on duplicate vaults and invalid risk parameters."""
    contract = test_env["contract"]
    UserError = test_env["mock_gl"].vm.UserError

    contract.onboard_collateral_vault(
        "V1", "SYM", "Issuer", "Valid reserve mandate string", 7000, 400, 1_000_000
    )

    with pytest.raises(UserError, match="Vault already onboarded"):
        contract.onboard_collateral_vault(
            "v1", "SYM", "Issuer", "Valid reserve mandate string", 7000, 400, 1_000_000
        )

    with pytest.raises(UserError, match="Invalid base_ltv_bps"):
        contract.onboard_collateral_vault(
            "V2", "SYM", "Issuer", "Valid reserve mandate string", 9900, 400, 1_000_000
        )
