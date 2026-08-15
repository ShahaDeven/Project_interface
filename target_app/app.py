"""Routes for the fake operator portal (DESIGN.md §2).

Deliberately dumb: server-rendered pages, no JSON API, no client-side routing,
no test hooks. The hostility lives in the templates; the determinism lives in
data.py; the injected runtime conditions live in chaos.py.
"""

import time

from flask import (
    Flask, g, redirect, render_template, request, session, url_for,
)

from . import chaos as chaos_mod
from . import data as d
from . import version as v

# Fixed dev-only key so sessions survive a server restart and evidence runs stay
# reproducible. Nothing here is a real secret and nothing real is protected by it.
DEV_SECRET_KEY = "target-app-dev-key-not-a-secret"

PUBLIC_ENDPOINTS = {"login", "favicon"}


def create_app():
    app = Flask(__name__)
    app.secret_key = DEV_SECRET_KEY
    # Templates are re-read on change. This is not debug mode: no reloader process,
    # no injected debugger markup, so evidence runs stay clean — it only spares us
    # a restart per template edit.
    app.config["TEMPLATES_AUTO_RELOAD"] = True
    app.jinja_env.globals["app_build"] = v.APP_BUILD
    app.jinja_env.globals["app_fingerprint"] = v.APP_FINGERPRINT
    app.jinja_env.globals["operator_name"] = d.OPERATOR_NAME
    app.jinja_env.globals["operator_region"] = d.OPERATOR_REGION
    app.jinja_env.globals["account_types"] = d.ACCOUNT_TYPES
    app.jinja_env.globals["funding_sources"] = d.FUNDING_SOURCES
    app.jinja_env.filters["money"] = d.money

    # ---------------------------------------------------------------- hooks --

    @app.before_request
    def chaos_gate():
        """Arm on the request that carries ?chaos=, fire on the one after it."""
        if request.endpoint not in chaos_mod.CHAOS_ELIGIBLE_ENDPOINTS:
            return None

        incoming = request.args.get("chaos")
        if incoming in chaos_mod.CHAOS_MODES:
            session[chaos_mod.SESSION_KEY] = incoming
            return None

        # Only a GET fires it. A POST here answers with a redirect, and a redirect
        # is not a page load — the page load is the GET that follows it. Firing on
        # the POST spends the flag on a response with no body, which loses an
        # injected dialog entirely and delays a redirect nobody is looking at.
        if request.method != "GET":
            return None

        armed = session.pop(chaos_mod.SESSION_KEY, None)
        if armed == "slow":
            time.sleep(chaos_mod.SLOW_SECONDS)
        elif armed == "error":
            return render_template("error500.html"), 500
        elif armed == "session":
            session.pop("operator", None)  # auth_gate below bounces to /login
        elif armed == "dialog":
            g.show_dialog = True
        return None

    @app.before_request
    def auth_gate():
        if request.endpoint in PUBLIC_ENDPOINTS or request.endpoint is None:
            return None
        if not session.get("operator"):
            return redirect(url_for("login"))
        return None

    @app.context_processor
    def inject_shell_state():
        return {"show_dialog": getattr(g, "show_dialog", False)}

    # --------------------------------------------------------------- routes --

    @app.route("/")
    def home():
        return redirect(url_for("search"))

    @app.route("/favicon.ico")
    def favicon():
        return ("", 204)

    @app.route("/login", methods=["GET", "POST"])
    def login():
        if request.method == "POST":
            # Any credentials are accepted; the operator identity is hardcoded.
            session["operator"] = d.OPERATOR_NAME
            return redirect(url_for("search"))
        return render_template("login.html")

    @app.route("/search")
    def search():
        raw = (request.args.get("txtMbr") or "").strip()
        if not raw:
            return render_template("search.html", entered="", error=None)
        if not d.is_valid_member_number(raw):
            return render_template(
                "search.html", entered=raw,
                error="Member number must be exactly 5 digits.",
            )
        if d.get_member(raw) is None:
            return render_template(
                "search.html", entered=raw,
                error="No member matches this number.",
            )
        return redirect(url_for("member_profile", member_id=raw))

    @app.route("/member/<member_id>")
    def member_profile(member_id):
        member = d.get_member(member_id)
        if member is None:
            return render_template("notfound.html", entered=member_id), 404
        if not d.in_operator_region(member):
            return render_template("denied.html", member_id=member_id,
                                   member_region=member["region"]), 403
        return render_template("member.html", m=member,
                               balance=d.effective_balance(member),
                               sub_accounts=d.sub_accounts_for(member_id))

    @app.route("/member/<member_id>/loan-frame")
    def loan_frame(member_id):
        """Loan panel, served into the profile page's iframe."""
        member = d.get_member(member_id)
        if member is None or not d.in_operator_region(member):
            return render_template("loan_frame.html", m=None), 404
        return render_template("loan_frame.html", m=member)

    @app.route("/member/<member_id>/sub-account/new", methods=["GET", "POST"])
    def subaccount_new(member_id):
        member = d.get_member(member_id)
        if member is None:
            return render_template("notfound.html", entered=member_id), 404
        if not d.in_operator_region(member):
            return render_template("denied.html", member_id=member_id,
                                   member_region=member["region"]), 403

        if request.method == "GET":
            return render_template("subaccount_form.html", m=member,
                                   form=_blank_form(), error=None)

        form = _read_form(request.form)
        error = _validate_form(form)
        if error:
            return render_template("subaccount_form.html", m=member,
                                   form=form, error=error)
        return render_template("subaccount_confirm.html", m=member, form=form)

    @app.route("/member/<member_id>/sub-account/confirm", methods=["POST"])
    def subaccount_confirm(member_id):
        member = d.get_member(member_id)
        if member is None:
            return render_template("notfound.html", entered=member_id), 404
        if not d.in_operator_region(member):
            return render_template("denied.html", member_id=member_id,
                                   member_region=member["region"]), 403

        form = _read_form(request.form)
        error = _validate_form(form)
        if error:
            return render_template("subaccount_form.html", m=member,
                                   form=form, error=error)

        # The irreversible step. Everything before this screen can be abandoned;
        # this is the one that leaves a mark the member's profile will show.
        opened = d.record_sub_account(
            member_id=member["id"],
            account_type=form["acct_type"],
            nickname=form["nickname"],
            deposit=float(form["deposit"].replace(",", "").replace("$", "")),
            funding=form["funding"],
        )
        return render_template("subaccount_done.html", m=member, form=form,
                               account_number=opened["number"])

    @app.errorhandler(500)
    def server_error(_exc):
        return render_template("error500.html"), 500

    return app


# ------------------------------------------------------------ form helpers --

def _blank_form():
    return {
        "acct_type": d.ACCOUNT_TYPES[0],
        "nickname": "",
        "deposit": "",
        "funding": d.FUNDING_SOURCES[0],
        "statements": "Electronic",
        "overdraft": False,
    }


def _read_form(src):
    return {
        "acct_type": (src.get("ddlActType") or "").strip(),
        "nickname": (src.get("txtNick") or "").strip(),
        "deposit": (src.get("txtDep") or "").strip(),
        "funding": (src.get("ddlFund") or "").strip(),
        "statements": (src.get("rdoStmt") or "").strip(),
        "overdraft": bool(src.get("chkOd")),
    }


def _validate_form(form):
    if form["acct_type"] not in d.ACCOUNT_TYPES:
        return "Select an account type."
    if not form["nickname"]:
        return "Account nickname is required."
    try:
        amount = float(form["deposit"].replace(",", "").replace("$", ""))
    except ValueError:
        return "Initial deposit is not a valid amount."
    if amount < 25:
        return "Initial deposit must be at least $25.00."
    if form["funding"] not in d.FUNDING_SOURCES:
        return "Select a funding source."
    if form["statements"] not in ("Mail", "Electronic"):
        return "Select a statement delivery method."
    return None


def serve(host="127.0.0.1", port=5000):
    """Entry point used by `python -m target_app` and later by the cua CLI."""
    create_app().run(host=host, port=port, debug=False, threaded=True)


if __name__ == "__main__":
    serve()
