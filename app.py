from flask import Flask, redirect, url_for, session, render_template
from config import Config

def create_app():
    app = Flask(__name__)
    app.config.from_object(Config)

    @app.after_request
    def prevent_html_caching(response):
        if response.mimetype == 'text/html':
            response.headers['Cache-Control'] = (
                'no-store, no-cache, must-revalidate, max-age=0, private'
            )
            response.headers['Pragma'] = 'no-cache'
            response.headers['Expires'] = '0'
        return response

    # Register Blueprints
    from routes.auth import auth_bp
    from routes.admin import admin_bp
    from routes.patient import patient_bp
    from routes.doctor import doctor_bp
    from routes.receptionist import receptionist_bp
    from routes.appointments import appointments_bp
    from routes.pharmacy import pharmacy_bp
    from routes.beds import beds_bp

    app.register_blueprint(auth_bp)
    app.register_blueprint(admin_bp)
    app.register_blueprint(patient_bp)
    app.register_blueprint(doctor_bp)
    app.register_blueprint(receptionist_bp)
    app.register_blueprint(appointments_bp)
    app.register_blueprint(pharmacy_bp)
    app.register_blueprint(beds_bp)

    @app.route('/')
    def index():
        if 'user_id' in session:
            role = session.get('role')
            if role == 'patient':
                return redirect(url_for('patient.dashboard'))
            elif role == 'doctor':
                return redirect(url_for('doctor.dashboard'))
            elif role == 'receptionist':
                return redirect(url_for('receptionist.dashboard'))
            elif role == 'admin':
                return redirect(url_for('admin.dashboard'))
        return redirect(url_for('auth.login'))

    @app.errorhandler(404)
    def page_not_found(e):
        return render_template('404.html'), 404

    @app.errorhandler(500)
    def internal_server_error(e):
        return render_template('500.html'), 500

    return app

app = create_app()

if __name__ == '__main__':
    print("Starting Smart Hospital Management System Flask Server...")
    app.run(host='0.0.0.0', port=5000, debug=True)
