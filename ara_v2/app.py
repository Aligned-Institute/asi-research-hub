"""
ARA v2 Application Factory
Creates and configures the Flask application with all extensions.
"""

from flask import Flask, jsonify, request, redirect
from flask_cors import CORS
from flask_talisman import Talisman

from ara_v2.config import get_config
from ara_v2.utils.logger import configure_logging
from ara_v2.utils.database import db, init_db
from ara_v2.utils.redis_client import redis_client, init_redis
from ara_v2.utils.errors import register_error_handlers


def create_app(config_name=None):
    """
    Application factory pattern.

    Args:
        config_name: Configuration to use (development, production, testing)

    Returns:
        Configured Flask application
    """
    import os
    root_path = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    app = Flask(__name__, static_folder=os.path.join(root_path, 'static'))

    # Load configuration
    if config_name:
        app.config.from_object(f'ara_v2.config.{config_name.capitalize()}Config')
    else:
        app.config.from_object(get_config())

    # Configure logging
    configure_logging(app)

    # Initialize extensions
    init_extensions(app)

    # Register blueprints
    register_blueprints(app)

    # Register error handlers
    register_error_handlers(app)

    # Health check endpoints
    register_health_checks(app)
    
    # Disable caching for static files to prevent CDN/browser caching issues
    @app.after_request
    def add_cache_control(response):
        if request.path.startswith('/static/'):
            response.headers['Cache-Control'] = 'no-cache, no-store, must-revalidate'
            response.headers['Pragma'] = 'no-cache'
            response.headers['Expires'] = '0'
        return response

    app.logger.info(f"ARA v2 started in {app.config.get('FLASK_ENV', 'unknown')} mode")

    return app


def init_extensions(app):
    """Initialize Flask extensions."""

    # Database
    init_db(app)

    # Redis
    init_redis(app)

    # CORS
    CORS(app, resources={
        r"/api/*": {
            "origins": app.config['CORS_ORIGINS'],
            "methods": ["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
            "allow_headers": ["Content-Type", "Authorization", "X-CSRF-Token"],
            "expose_headers": ["X-Request-ID"],
            "supports_credentials": True
        }
    })

    # Security Headers (only in production)
    if not app.debug:
        # Disable CSP for now to allow legacy frontend scripts
        Talisman(
            app,
            force_https=False,
            strict_transport_security=True,
            content_security_policy=None
        )

    # Rate Limiting (import here to avoid circular dependency)
    from ara_v2.utils.rate_limiter import limiter, init_limiter
    init_limiter(app)

    # Prometheus Metrics
    if app.config.get('ENABLE_PROMETHEUS_METRICS'):
        from ara_v2.utils.metrics import init_metrics
        init_metrics(app)

    app.logger.info("All extensions initialized")


def register_blueprints(app):
    """Register Flask blueprints (API routes)."""

    # Import blueprints
    from ara_v2.api.endpoints.auth import auth_bp
    from ara_v2.api.endpoints.papers import papers_bp
    from ara_v2.api.endpoints.bookmarks import bookmarks_bp
    from ara_v2.api.endpoints.tags import tags_bp
    from ara_v2.api.endpoints.metrics import metrics_bp
    # from ara_v2.api.endpoints.mindmap import mindmap_bp  # Phase 3
    # from ara_v2.api.endpoints.export import export_bp  # Phase 4
    # from ara_v2.api.endpoints.analytics import analytics_bp  # Phase 5
    # from ara_v2.api.endpoints.admin import admin_bp  # Phase 1+

    # Register blueprints
    app.register_blueprint(auth_bp, url_prefix='/api')
    app.register_blueprint(papers_bp, url_prefix='/api/papers')
    app.register_blueprint(bookmarks_bp, url_prefix='/api/bookmarks')
    app.register_blueprint(tags_bp, url_prefix='/api/tags')
    app.register_blueprint(metrics_bp, url_prefix='/api/metrics')

    app.logger.info("All blueprints registered")


def register_health_checks(app):
    """Register health check endpoints."""

    @app.route('/health', methods=['GET'])
    def health_check():
        """Basic health check."""
        return jsonify({'status': 'healthy'}), 200

    @app.route('/health/detailed', methods=['GET'])
    def detailed_health_check():
        """Detailed health check with dependency status."""
        from datetime import datetime
        from sqlalchemy import text

        health_status = {
            'status': 'healthy',
            'timestamp': datetime.utcnow().isoformat(),
            'checks': {}
        }

        # Check database
        try:
            db.session.execute(text('SELECT 1'))
            health_status['checks']['database'] = {'status': 'healthy'}
        except Exception as e:
            health_status['status'] = 'unhealthy'
            health_status['checks']['database'] = {
                'status': 'unhealthy',
                'error': str(e)
            }

        # Check Redis
        try:
            redis_client.ping()
            health_status['checks']['redis'] = {'status': 'healthy'}
        except Exception as e:
            health_status['status'] = 'degraded'
            health_status['checks']['redis'] = {
                'status': 'unhealthy',
                'error': str(e)
            }

        status_code = {
            'healthy': 200,
            'degraded': 200,
            'unhealthy': 503
        }[health_status['status']]

        return jsonify(health_status), status_code

    @app.route('/', methods=['GET'])
    def root():
        """Root endpoint."""
        return redirect("/static/login.html")


if __name__ == '__main__':
    app = create_app()
    app.run(host='0.0.0.0', port=5000)
