"""Run independent acceptance journeys and retain an honest aggregate result."""
import json
import traceback


class Gates:
    def __init__(self, destination):
        self.destination = destination
        self.results = {}

    def record(self, name, status, **details):
        self.results[name] = {'status': status, **details}
        self.destination.write_text(json.dumps(self.results, indent=2) + '\n')
        print(f'GATE {name}: {status}', flush=True)

    def run(self, name, action):
        try:
            action()
        except Exception as error:
            # Tracebacks remain in the credential-redacted CI log. The portable
            # receipt records types, never arbitrary exception payloads.
            traceback.print_exc()
            self.record(name, 'failed', error_type=type(error).__name__)
            return False
        self.record(name, 'passed')
        return True

    def blocked(self, name, dependency):
        self.record(name, 'blocked', dependency=dependency)

    def finish(self):
        failed = [name for name, result in self.results.items() if result['status'] != 'passed']
        if failed:
            raise RuntimeError('Acceptance gates did not pass: ' + ', '.join(failed))
