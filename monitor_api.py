"""
monitor_api.py
──────────────
New REST endpoints for the automated monitoring service. Plug into the
existing Flask app with two lines in app.py:

    from monitor_api import register_monitor_routes
    register_monitor_routes(app)

Endpoints
─────────
  GET  /api/monitor/status       Service status
  POST /api/monitor/start        Start automated monitoring
  POST /api/monitor/stop         Stop automated monitoring

  GET  /api/config               Current config (secrets masked)
  POST /api/config               Update config (JSON body)
  POST /api/config/validate      Validate config
  POST /api/config/test-network  Test network credentials

  GET  /api/queues               All queues + their files
  GET  /api/analytics            Per-file records (paginated + filtered)
                                 query: page, page_size, search, status,
                                        doc_type, state, county
  GET  /api/analytics/summary    Status counts, average duration, filter facets
  POST /api/analytics/clear      Reset analytics history
  GET  /api/file/<file_id>       Stream the original PDF inline (for preview)
"""

from __future__ import annotations

from pathlib import Path
from flask import request, jsonify, send_file, abort

from config          import get_config, update_config, BASE_DIR
from monitor_service import monitor
from analytics_store import analytics, STAGES


def register_monitor_routes(app):

    # ── Monitor control ─────────────────────────────────────────────────
    @app.route("/api/monitor/status", methods=["GET"])
    def monitor_status():
        return jsonify(monitor.status())

    @app.route("/api/monitor/start", methods=["POST"])
    def monitor_start():
        try:
            return jsonify(monitor.start())
        except Exception as e:
            return jsonify({"error": str(e)}), 400

    @app.route("/api/monitor/stop", methods=["POST"])
    def monitor_stop():
        return jsonify(monitor.stop())

    # ── Config ──────────────────────────────────────────────────────────
    @app.route("/api/config", methods=["GET"])
    def config_get():
        return jsonify(get_config().as_public_dict())

    @app.route("/api/config", methods=["POST"])
    def config_set():
        changes = request.get_json(silent=True) or {}
        network_password = changes.pop("network_password", None)
        
        cfg, error = update_config(changes, network_password)
        
        if error:
            return jsonify({"ok": False, "error": error}), 400
        
        return jsonify({"ok": True, "config": cfg.as_public_dict()})

    @app.route("/api/config/validate", methods=["POST"])
    def config_validate():
        """
        Validate configuration without saving.
        POST body: {source_type, source_path, network_username, ...}
        """
        data = request.get_json(silent=True) or {}
        
        cfg = get_config()
        
        # Apply changes temporarily for validation
        for k, v in data.items():
            if hasattr(cfg, k):
                setattr(cfg, k, v)
        
        is_valid, error = cfg.validate()
        
        if is_valid:
            return jsonify({"ok": True, "valid": True})
        else:
            return jsonify({"ok": False, "valid": False, "error": error}), 400

    @app.route("/api/config/test-network", methods=["POST"])
    def config_test_network():
        """
        Test network credentials without saving configuration.
        POST body: {
            source_path: "\\\\server\\share\\path",
            network_username: "domain\\username",
            network_password: "password"
        }
        """
        data = request.get_json(silent=True) or {}
        
        unc_path = data.get("source_path", "").strip()
        username = data.get("network_username", "").strip()
        password = data.get("network_password", "").strip()
        domain = data.get("network_domain", "").strip()
        
        if not unc_path:
            return jsonify({
                "ok": False,
                "error": "source_path required"
            }), 400
        
        if not username:
            return jsonify({
                "ok": False,
                "error": "network_username required"
            }), 400
        
        if not password:
            return jsonify({
                "ok": False,
                "error": "network_password required"
            }), 400
        
        try:
            from network_auth import test_network_credentials
            
            is_valid, error = test_network_credentials(
                unc_path,
                username,
                password,
                domain
            )
            
            if is_valid:
                return jsonify({
                    "ok": True,
                    "valid": True,
                    "message": f"Successfully authenticated to {unc_path}"
                })
            else:
                return jsonify({
                    "ok": False,
                    "valid": False,
                    "error": error
                }), 400
        
        except Exception as e:
            return jsonify({
                "ok": False,
                "error": f"Network test failed: {str(e)}"
            }), 500

    @app.route("/api/config/save-network-credentials", methods=["POST"])
    def config_save_network_credentials():
        """
        Save network credentials securely (separate from config update).
        POST body: {
            network_username: "domain\\username",
            network_password: "password",
            network_domain: "optional_domain"
        }
        """
        data = request.get_json(silent=True) or {}
        
        username = data.get("network_username", "").strip()
        password = data.get("network_password", "").strip()
        domain = data.get("network_domain", "").strip()
        
        if not username or not password:
            return jsonify({
                "ok": False,
                "error": "username and password required"
            }), 400
        
        try:
            from network_auth import store_network_credentials
            
            store_network_credentials(username, password, domain or None)
            
            return jsonify({
                "ok": True,
                "message": "Credentials saved securely"
            })
        
        except Exception as e:
            return jsonify({
                "ok": False,
                "error": f"Failed to save credentials: {str(e)}"
            }), 500

    # ── Queues ──────────────────────────────────────────────────────────
    @app.route("/api/queues", methods=["GET"])
    def queues_get():
        return jsonify({"queues": monitor.queues()})

    # ── Analytics ───────────────────────────────────────────────────────
    @app.route("/api/analytics", methods=["GET"])
    def analytics_get():
        q = request.args
        result = analytics.query(
            page      = int(q.get("page", 1) or 1),
            page_size = int(q.get("page_size", 50) or 50),
            search    = q.get("search", ""),
            status    = q.get("status", ""),
            doc_type  = q.get("doc_type", ""),
            state     = q.get("state", ""),
            county    = q.get("county", ""),
        )
        result["stages"] = STAGES
        return jsonify(result)

    @app.route("/api/analytics/summary", methods=["GET"])
    def analytics_summary():
        return jsonify(analytics.summary())

    @app.route("/api/analytics/clear", methods=["POST"])
    def analytics_clear():
        analytics.clear()
        return jsonify({"ok": True})

    # ── Original PDF (inline preview, no download prompt) ────────────────
    # Allowed roots: the project output/, input_pdfs/, and FTP/cloud staging.
    _ALLOWED_ROOTS = [
        (BASE_DIR / "output").resolve(),
        (BASE_DIR / "input_pdfs").resolve(),
        (BASE_DIR / "temp").resolve(),
    ]

    def _is_allowed(path: Path) -> bool:
        try:
            rp = path.resolve()
        except Exception:
            return False
        for root in _ALLOWED_ROOTS:
            try:
                rp.relative_to(root)        # raises ValueError if outside
                return True
            except ValueError:
                continue
        return False

    @app.route("/api/file/<file_id>", methods=["GET"])
    def serve_file(file_id):
        rec = analytics.get_record(file_id)
        if not rec or not rec.output_path:
            abort(404, description="No file recorded for this id yet.")
        path = Path(rec.output_path)
        if not path.exists() or not _is_allowed(path):
            abort(404, description="File not found.")
        # inline so the browser's PDF viewer opens it (zoom + page nav) instead
        # of downloading.
        return send_file(str(path), mimetype="application/pdf",
                         as_attachment=False, download_name=path.name)

    # ── Optional autostart on boot ──────────────────────────────────────
    if get_config().autostart:
        try:
            monitor.start()
        except Exception as e:
            app.logger.warning(f"Monitor autostart failed: {e}")

    return app
