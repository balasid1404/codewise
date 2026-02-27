"""Example usage of the fault localizer."""

from fault_localizer import FaultLocalizer

# Example Python stack trace
PYTHON_ERROR = """
Traceback (most recent call last):
  File "app/main.py", line 45, in handle_request
    result = processor.process(data)
  File "app/processor.py", line 23, in process
    validated = self.validator.validate(data)
  File "app/validator.py", line 67, in validate
    return self._check_schema(data)
  File "app/validator.py", line 89, in _check_schema
    raise ValueError(f"Invalid field: {field}")
ValueError: Invalid field: user_email
"""

# Example Java stack trace
JAVA_ERROR = """
java.lang.NullPointerException: Cannot invoke method on null object
    at com.example.service.UserService.getUser(UserService.java:45)
    at com.example.controller.UserController.handleRequest(UserController.java:23)
    at com.example.framework.Router.dispatch(Router.java:112)
    at com.example.framework.Application.run(Application.java:67)
"""


def main():
    # Initialize with path to your codebase
    localizer = FaultLocalizer(
        codebase_path="./sample-repo",
        use_llm=False  # Set True if you have AWS credentials configured
    )

    # Index the codebase (do once, cache results in production)
    print("Indexing codebase...")
    num_entities = localizer.index()
    print(f"Indexed {num_entities} code entities")

    # Localize fault from Python error
    print("\n--- Python Fault Localization ---")
    results = localizer.localize(PYTHON_ERROR)

    for i, result in enumerate(results, 1):
        entity = result["entity"]
        print(f"\n{i}. {entity.full_name}")
        print(f"   File: {entity.file_path}:{entity.start_line}")
        print(f"   Confidence: {result.get('confidence', 'N/A')}")
        print(f"   Reason: {result.get('reason', 'N/A')}")

    # Localize fault from Java error
    print("\n--- Java Fault Localization ---")
    results = localizer.localize(JAVA_ERROR)

    for i, result in enumerate(results, 1):
        entity = result["entity"]
        print(f"\n{i}. {entity.full_name}")
        print(f"   File: {entity.file_path}:{entity.start_line}")
        print(f"   Confidence: {result.get('confidence', 'N/A')}")
        print(f"   Reason: {result.get('reason', 'N/A')}")


if __name__ == "__main__":
    main()
