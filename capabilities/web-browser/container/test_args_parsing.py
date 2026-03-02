"""
Tests for the Invoke method's argument parsing resilience.

Validates that the server correctly handles:
1. Normal JSON object args (the happy path)
2. A bare JSON string (e.g. '"https://example.com"') — the Bedrock streaming
   fallback scenario
3. Completely invalid JSON (raw bytes that aren't valid JSON)
4. Non-dict JSON types that don't map to a primary param (should error)
5. Empty args (should default to {})

This test mocks out gRPC protobuf types and Playwright so it can run without
those dependencies installed.
"""

import json
import sys
import types

# ---------------------------------------------------------------------------
# Mock out heavy dependencies so we can import server.py in a lightweight way
# ---------------------------------------------------------------------------

# Mock capability_pb2
pb2 = types.ModuleType("capability_pb2")

class _MockInvokeResponse:
    def __init__(self, result_json=None, error=None):
        self.result_json = result_json or b""
        self.error = error or ""

class _MockHealthResponse:
    def __init__(self, ready=True, message="ok"):
        self.ready = ready
        self.message = message

class _MockInvokeChunk:
    def __init__(self, data=None, done=False, error=None):
        self.data = data
        self.done = done
        self.error = error

pb2.InvokeResponse = _MockInvokeResponse
pb2.HealthResponse = _MockHealthResponse
pb2.InvokeChunk = _MockInvokeChunk
sys.modules["capability_pb2"] = pb2

# Mock capability_pb2_grpc
pb2_grpc = types.ModuleType("capability_pb2_grpc")

class _MockCapabilityServicer:
    pass

pb2_grpc.CapabilityServicer = _MockCapabilityServicer
pb2_grpc.add_CapabilityServicer_to_server = lambda servicer, server: None
sys.modules["capability_pb2_grpc"] = pb2_grpc

# Mock grpc
mock_grpc = types.ModuleType("grpc")
mock_grpc.server = lambda *a, **kw: None
sys.modules["grpc"] = mock_grpc

# Mock playwright
pw_module = types.ModuleType("playwright")
pw_sync = types.ModuleType("playwright.sync_api")
pw_sync.sync_playwright = lambda: None
pw_sync.Browser = type("Browser", (), {})
pw_sync.BrowserContext = type("BrowserContext", (), {})
pw_sync.Page = type("Page", (), {})
pw_sync.Playwright = type("Playwright", (), {})
pw_sync.TimeoutError = type("TimeoutError", (Exception,), {})
sys.modules["playwright"] = pw_module
sys.modules["playwright.sync_api"] = pw_sync

# ---------------------------------------------------------------------------
# Now import the server module
# ---------------------------------------------------------------------------

import server  # noqa: E402

# ---------------------------------------------------------------------------
# Test helpers
# ---------------------------------------------------------------------------

class FakeRequest:
    """Mimics a gRPC InvokeRequest."""
    def __init__(self, tool_name: str, args_json):
        self.tool_name = tool_name
        # In real gRPC, args_json is bytes
        if isinstance(args_json, str):
            args_json = args_json.encode("utf-8")
        self.args_json = args_json


class FakeContext:
    pass


# Replace browser-dependent handlers with simple echo handlers for testing
def _echo_handler(args: dict) -> str:
    return json.dumps({"echo": args})

for tool_name in server.TOOL_HANDLERS:
    server.TOOL_HANDLERS[tool_name] = _echo_handler


servicer = server.CapabilityServicer()
ctx = FakeContext()

passed = 0
failed = 0

def assert_eq(test_name, actual, expected):
    global passed, failed
    if actual == expected:
        passed += 1
        print(f"  PASS: {test_name}")
    else:
        failed += 1
        print(f"  FAIL: {test_name}")
        print(f"    expected: {expected!r}")
        print(f"    actual:   {actual!r}")

def assert_no_error(test_name, resp):
    global passed, failed
    if not resp.error:
        passed += 1
        print(f"  PASS: {test_name} (no error)")
    else:
        failed += 1
        print(f"  FAIL: {test_name} (unexpected error: {resp.error})")

def assert_has_error(test_name, resp):
    global passed, failed
    if resp.error:
        passed += 1
        print(f"  PASS: {test_name} (got expected error: {resp.error})")
    else:
        failed += 1
        print(f"  FAIL: {test_name} (expected an error but got none)")


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

print("\n=== Test 1: Normal JSON object args (happy path) ===")
req = FakeRequest("navigate", json.dumps({"url": "https://example.com"}))
resp = servicer.Invoke(req, ctx)
assert_no_error("navigate with dict args", resp)
result = json.loads(resp.result_json)
assert_eq("url passed through", result["echo"]["url"], "https://example.com")

print("\n=== Test 2: Bare JSON string — the Bedrock fallback scenario ===")
# This is the exact bug: orchestrator sends '"https://example.com"' as args_json
# json.loads('"https://example.com"') -> str, not dict
req = FakeRequest("navigate", '"https://example.com"')
resp = servicer.Invoke(req, ctx)
assert_no_error("navigate with bare string args", resp)
result = json.loads(resp.result_json)
assert_eq("url recovered from bare string", result["echo"]["url"], "https://example.com")

print("\n=== Test 3: Invalid JSON (not parseable at all) ===")
# The orchestrator might send completely broken bytes
req = FakeRequest("navigate", "https://example.com")  # no quotes = not valid JSON
resp = servicer.Invoke(req, ctx)
assert_no_error("navigate with invalid JSON", resp)
result = json.loads(resp.result_json)
assert_eq("url recovered from raw bytes", result["echo"]["url"], "https://example.com")

print("\n=== Test 4: Invalid JSON for tool without primary param ===")
req = FakeRequest("go_back", "some random string")
resp = servicer.Invoke(req, ctx)
assert_has_error("go_back with invalid JSON and no primary param", resp)

print("\n=== Test 5: Bare string for tool without primary param ===")
req = FakeRequest("go_back", '"some random string"')
resp = servicer.Invoke(req, ctx)
assert_has_error("go_back with bare string and no primary param", resp)

print("\n=== Test 6: Empty args_json ===")
req = FakeRequest("go_back", "")
req.args_json = b""  # empty bytes
resp = servicer.Invoke(req, ctx)
assert_no_error("go_back with empty args", resp)
result = json.loads(resp.result_json)
assert_eq("empty args gives empty dict", result["echo"], {})

print("\n=== Test 7: Other tools with primary params ===")
for tool, param, value in [
    ("press_key", "key", "Enter"),
    ("scroll", "direction", "down"),
    ("wait", "selector", "#loading"),
    ("execute_javascript", "script", "return document.title"),
    ("fill", "value", "Hello World"),
]:
    req = FakeRequest(tool, json.dumps(value))  # bare JSON string
    resp = servicer.Invoke(req, ctx)
    assert_no_error(f"{tool} with bare string", resp)
    result = json.loads(resp.result_json)
    assert_eq(f"{tool} param '{param}' recovered", result["echo"][param], value)

print("\n=== Test 8: JSON number (non-dict, non-string) for navigate ===")
req = FakeRequest("navigate", "42")
resp = servicer.Invoke(req, ctx)
assert_no_error("navigate with JSON number", resp)
result = json.loads(resp.result_json)
assert_eq("number wrapped as url", result["echo"]["url"], 42)

print("\n=== Test 9: JSON array for tool without primary param ===")
req = FakeRequest("load_state", '[1, 2, 3]')
resp = servicer.Invoke(req, ctx)
assert_has_error("load_state with JSON array and no primary param", resp)

# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------

print(f"\n{'='*50}")
print(f"Results: {passed} passed, {failed} failed")
print(f"{'='*50}\n")

sys.exit(1 if failed else 0)
