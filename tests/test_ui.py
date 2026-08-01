"""Invariants of the bundled sandboxd dashboard.

The dashboard is one hand-maintained HTML file with its script inline, so the
things that break silently are a lookup for an element that was renamed away and
a resource that quietly needs the network. Both are cheap to pin here.
"""

import re

import pytest

from pydantic_ai_backends.remote.server import _UI_FILE

PAGE = _UI_FILE.read_text(encoding="utf-8")

DECLARED_IDS = set(re.findall(r'\bid="([^"]+)"', PAGE))


class TestSelfContained:
    """No build step, no CDN — the promise that lets it work behind a strict CSP."""

    @pytest.mark.parametrize(
        ("pattern", "what"),
        [
            (r"<link\b", "a stylesheet or preload link"),
            (r"<script\s[^>]*\bsrc=", "an external script"),
            (r"@import", "a CSS import"),
            (r"https?://", "an absolute URL"),
        ],
    )
    def test_the_page_pulls_in_nothing(self, pattern: str, what: str):
        assert not re.search(pattern, PAGE), f"the dashboard now references {what}"

    def test_it_is_one_file_with_inline_style_and_script(self):
        assert PAGE.count("<style>") == 1
        assert PAGE.count("<script>") == 1


class TestScriptWiring:
    """Every element the script reaches for has to exist in the markup."""

    def test_every_lookup_resolves(self):
        looked_up = set(re.findall(r'\$\("([^"]+)"\)', PAGE))

        assert looked_up <= DECLARED_IDS, sorted(looked_up - DECLARED_IDS)

    def test_ids_are_unique(self):
        all_ids = re.findall(r'\bid="([^"]+)"', PAGE)

        assert len(all_ids) == len(set(all_ids))

    @pytest.mark.parametrize(
        "attribute", ["data-view-tab", "data-view", "data-pane-tab", "data-pane"]
    )
    def test_every_queried_hook_is_in_the_markup(self, attribute: str):
        """A view or pane the script routes to must have something to show."""
        assert f'querySelectorAll("[{attribute}]")' in PAGE
        assert re.search(rf'\b{attribute}="', PAGE)

    def test_the_view_and_pane_hooks_pair_up(self):
        """A tab with no panel, or a panel with no tab, is a dead end."""
        pairs = (("data-view-tab", "data-view"), ("data-pane-tab", "data-pane"))
        for tab_attr, panel_attr in pairs:
            tabs = set(re.findall(rf'{tab_attr}="([^"]+)"', PAGE))
            panels = set(re.findall(rf'\b{panel_attr}="([^"]+)"', PAGE))
            assert tabs == panels, f"{tab_attr} vs {panel_attr}: {tabs ^ panels}"


class TestSessionsTable:
    """How a session gets opened — the one path that is easy to lose."""

    def test_the_whole_row_opens_its_session(self):
        """Clicking only the id text is too small a target to find, which left
        creating a session as the only discoverable way into a workspace."""
        assert 'tr.addEventListener("click"' in PAGE
        assert "tbody tr { cursor: pointer;" in PAGE

    def test_the_row_click_does_not_swallow_its_buttons(self):
        assert 'event.target.closest("[data-kill], [data-open]")' in PAGE

    def test_the_id_stays_a_real_button_for_keyboard_users(self):
        assert 'class="row-open" type="button"' in PAGE


class TestOperatorGuidance:
    """The ceilings are read-only here, so the page has to say where they live."""

    def test_it_names_where_the_ceilings_are_configured(self):
        assert "SandboxdConfig" in PAGE
        assert "SandboxRuntime" in PAGE
        assert "SUGGESTED_RUNTIMES" in PAGE

    def test_it_explains_why_there_is_no_form(self):
        """An operator hunting for a memory field needs the answer, not silence."""
        assert "default, not a maximum" in PAGE
        assert "config change and a restart" in PAGE

    def test_the_create_form_says_ceilings_come_from_the_runtime(self):
        assert "set per runtime by the operator, not per session" in PAGE

    def test_the_create_form_covers_every_request_field(self):
        """A field added to the request should force a decision about the form.

        Pinning the set here means a new one fails this test rather than quietly
        being unreachable from the dashboard.
        """
        from pydantic_ai_backends.remote import wire

        assert set(wire.CreateSessionRequest.model_fields) == {
            "session_id",
            "runtime",
            "tenant",
            "reuse",
        }
        assert "{ runtime: el.newRuntime.value }" in PAGE
        assert "body.session_id = id" in PAGE
        assert "body.tenant = tenant" in PAGE
        assert "body.reuse = true" in PAGE

    def test_no_ceiling_is_offered_as_a_request_field(self):
        """A client naming its own memory would be a host takeover by another route."""
        for forbidden in ("body.mem_limit", "body.cpus", "body.pids_limit", "body.network_mode"):
            assert forbidden not in PAGE


class TestAccessibleStructure:
    """The tab widgets are hand-built, so their ARIA has to be complete."""

    def test_aria_controls_point_at_real_elements(self):
        targets = set(re.findall(r'aria-controls="([^"]+)"', PAGE))

        assert targets <= DECLARED_IDS, sorted(targets - DECLARED_IDS)

    def test_every_tab_declares_its_selected_state(self):
        tabs = re.findall(r"<button[^>]*\brole=\"tab\"[^>]*>", PAGE)

        assert tabs
        for tab in tabs:
            assert "aria-selected=" in tab
            assert "aria-controls=" in tab

    def test_there_is_exactly_one_main_landmark(self):
        assert PAGE.count("<main>") == 1

    def test_the_page_declares_a_language_and_a_title(self):
        assert '<html lang="en">' in PAGE
        assert "<title>sandboxd</title>" in PAGE

    def test_motion_is_guarded(self):
        assert "prefers-reduced-motion" in PAGE


class TestWireCoupling:
    """The dashboard reads specific JSON fields, and a rename would go unnoticed.

    A field the page asks for and the service no longer sends does not raise —
    it renders as an em dash, which is indistinguishable from "not sampled".
    Listing the coupling here makes the rename fail loudly instead.
    """

    def test_the_policy_fields_it_reads_all_exist(self):
        from pydantic_ai_backends.remote import wire

        read = {
            "runtimes",
            "default_runtime",
            "max_sessions",
            "max_sessions_per_tenant",
            "evict_idle_after",
            "mem_limit",
            "cpus",
            "pids_limit",
            "network_mode",
            "work_dir",
            "idle_timeout",
            "execute_timeout",
            "max_read_bytes",
            "persist_containers",
            "workspace_ttl",
            "container_ttl",
            "tmpfs_size",
            "prewarm",
            "buildkit",
            "oci_runtime",
        }

        assert read <= set(wire.ServicePolicy.model_fields)

    def test_the_runtime_fields_it_reads_all_exist(self):
        from pydantic_ai_backends.remote import wire

        read = {
            "alias",
            "image",
            "description",
            "builds",
            "mem_limit",
            "cpus",
            "pids_limit",
            "network_mode",
        }

        assert read <= set(wire.RuntimePolicy.model_fields)

    def test_the_session_fields_it_reads_all_exist(self):
        from pydantic_ai_backends.remote import wire

        read = {
            "session_id",
            "runtime",
            "tenant",
            "alive",
            "state",
            "created_at",
            "last_activity",
            "idle_seconds",
            "usage",
        }

        assert read <= set(wire.SessionInfo.model_fields)

    def test_the_session_states_it_branches_on_are_the_wire_ones(self):
        """A renamed state would silently badge every asleep session as dead."""
        from typing import get_args

        from pydantic_ai_backends.remote import wire

        states = get_args(wire.SessionInfo.model_fields["state"].annotation)

        assert "hibernated" in states
        for state in states:
            assert f'"{state}"' in PAGE or f">{state}<" in PAGE

    def test_the_usage_fields_it_reads_all_exist(self):
        from pydantic_ai_backends.remote import wire

        read = {"memory_bytes", "memory_limit_bytes", "cpu_percent", "pids"}

        assert read <= set(wire.SessionUsage.model_fields)

    def test_the_event_fields_it_reads_all_exist(self):
        from pydantic_ai_backends.remote import wire

        read = {"at", "op", "target", "detail", "duration_ms", "ok"}

        assert read <= set(wire.SessionEvent.model_fields)

    def test_every_route_it_calls_is_registered(self):
        """The page builds its URLs by hand, so the two lists have to agree."""
        from pydantic_ai_backends.remote.server import SandboxdConfig, create_app

        app = create_app(SandboxdConfig(token="t"))
        registered = {getattr(route, "path", "") for route in app.routes}

        assert {
            "/healthz",
            "/policy",
            "/sessions",
            "/sessions/{session_id}",
            "/sessions/{session_id}/exec",
            "/sessions/{session_id}/ls",
            "/sessions/{session_id}/read",
            "/sessions/{session_id}/events",
            "/workspaces/{session_id}/ls",
            "/workspaces/{session_id}/read",
        } <= registered

    @pytest.mark.parametrize(
        "fragment",
        ["/healthz", "/policy", '"/sessions', '"/workspaces/', "/exec", "/events?after="],
    )
    def test_the_page_still_reaches_for_each_of_them(self, fragment: str):
        assert fragment in PAGE
