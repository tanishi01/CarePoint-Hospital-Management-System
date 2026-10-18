# CarePoint – Hospital Management System

CarePoint is a Flask and MySQL-based hospital management system designed to simplify hospital operations through a centralized web application.

## Features

- **Role-Based Access:** Separate portals for administrators, doctors, patients, and receptionists.
- **Appointment Management:** Doctor schedules, appointment booking, and cancellation.
- **Patient Management:** Patient profiles, medical records, and billing.
- **Pharmacy Management:** Medicine inventory and low-stock alerts.
- **Hospital Administration:** Staff management, doctor schedules, leave requests, and bed availability.
- **Password Recovery:** Email-based password reset functionality.

## Tech Stack

- **Backend:** Python, Flask
- **Frontend:** HTML, CSS, JavaScript, Jinja2
- **Database:** MySQL
- **Libraries:** PyMySQL, Werkzeug, python-dotenv
- **Testing:** Python unittest

## Setup

1. Clone the repository:

   ```bash
   git clone https://github.com/tanishi01/CarePoint-Hospital-Management-System.git
   cd CarePoint-Hospital-Management-System
   ```

2. Create and activate a virtual environment.

3. Install dependencies:

   ```bash
   pip install -r requirements.txt
   ```

4. Configure your local `.env` file using `.env.example`.

5. Set up the MySQL database and required tables.

6. Run the application using the Flask entry point configured in `app.py`.

## Testing

Run the automated tests:

```bash
python -m unittest discover -s tests
```

## Author

**Tanishi Shukla**  
B.Tech – Computer Engineering

GitHub: [@tanishi01](https://github.com/tanishi01)

## Disclaimer

This project is developed for educational purposes and is not intended for production hospital use without further security, privacy, and reliability testing.
