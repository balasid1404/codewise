"""Sample application for testing fault localization."""

from processor import DataProcessor


class Application:
    def __init__(self):
        self.processor = DataProcessor()

    def handle_request(self, data: dict) -> dict:
        """Handle incoming request."""
        result = self.processor.process(data)
        return {"status": "success", "data": result}

    def run(self):
        """Run the application."""
        test_data = {"user_email": "invalid"}
        return self.handle_request(test_data)


if __name__ == "__main__":
    app = Application()
    app.run()
