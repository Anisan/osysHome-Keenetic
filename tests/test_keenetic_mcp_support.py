"""Tests for Keenetic MCP support."""

import unittest

from plugins.Keenetic import mcp_support


class TestKeeneticMcpSupport(unittest.TestCase):
    def test_capabilities(self):
        caps = mcp_support.mcp_capabilities()
        self.assertTrue(caps.get("entities"))
        self.assertTrue(caps.get("notes"))
        collection_ids = {item["id"] for item in caps.get("collections", [])}
        self.assertEqual(collection_ids, {"routers", "devices", "vpn", "log_rules", "journal"})
        routers = next(item for item in caps["collections"] if item["id"] == "routers")
        self.assertEqual(routers.get("binding_mode"), "object")
        self.assertIn("linked_object", routers.get("writable_fields", []))
        self.assertIn("has_linked_object", routers.get("list_filters", []))
        devices = next(item for item in caps["collections"] if item["id"] == "devices")
        self.assertIn("router_id", devices.get("list_filters", []))
        vpn = next(item for item in caps["collections"] if item["id"] == "vpn")
        self.assertEqual(vpn.get("binding_mode"), "object")
        self.assertIn("poll_now", caps.get("operations", []))
        self.assertIn("check_firmware", caps.get("operations", []))
        self.assertIn("vpn_connect", caps.get("operations", []))

    def test_config_schema(self):
        schema = mcp_support.mcp_config_schema()
        self.assertIn("interval", schema.get("properties", {}))
        self.assertIn("firmware_check_interval", schema.get("properties", {}))
        self.assertIn("journal_buffer_limit", schema.get("properties", {}))
        self.assertFalse(schema.get("additionalProperties", True))

    def test_entity_schemas(self):
        routers_schema = mcp_support.mcp_entity_schema("routers")
        self.assertIn("linked_object", routers_schema.get("properties", {}))
        self.assertTrue(routers_schema.get("properties", {}).get("password", {}).get("writeOnly"))
        self.assertTrue(routers_schema.get("properties", {}).get("model", {}).get("readOnly"))
        devices_schema = mcp_support.mcp_entity_schema("devices")
        self.assertTrue(devices_schema.get("properties", {}).get("router_id", {}).get("readOnly"))
        self.assertTrue(devices_schema.get("properties", {}).get("mac", {}).get("readOnly"))
        self.assertEqual(devices_schema.get("required"), [])
        vpn_schema = mcp_support.mcp_entity_schema("vpn")
        self.assertIn("linked_object", vpn_schema.get("properties", {}))
        self.assertIn("linked_method", vpn_schema.get("properties", {}))
        self.assertTrue(vpn_schema.get("properties", {}).get("key", {}).get("readOnly"))
        self.assertTrue(vpn_schema.get("properties", {}).get("router_id", {}).get("readOnly"))
        self.assertEqual(vpn_schema.get("required"), [])
        vpn = next(item for item in mcp_support.mcp_capabilities()["collections"] if item["id"] == "vpn")
        self.assertIn("linked_method", vpn.get("writable_fields", []))

    def test_validate_router_requires_title_ip(self):
        result = mcp_support.mcp_validate_entity("routers", {"title": "R1"})
        self.assertFalse(result.get("ok"))
        result = mcp_support.mcp_validate_entity("routers", {"ip": "192.168.1.1"})
        self.assertFalse(result.get("ok"))

    def test_validate_router_rejects_readonly_fields(self):
        result = mcp_support.mcp_validate_entity(
            "routers",
            {"title": "R1", "ip": "192.168.1.1", "online": 1},
        )
        self.assertFalse(result.get("ok"))

    def test_validate_router_port_range(self):
        result = mcp_support.mcp_validate_entity(
            "routers",
            {"title": "R1", "ip": "192.168.1.1", "port": 0},
        )
        self.assertFalse(result.get("ok"))
        result = mcp_support.mcp_validate_entity(
            "routers",
            {"title": "R1", "ip": "192.168.1.1", "port": 80},
        )
        self.assertTrue(result.get("ok"))

    def test_validate_device_rejects_create_without_id(self):
        result = mcp_support.mcp_validate_entity("devices", {"title": "Phone"})
        self.assertFalse(result.get("ok"))

    def test_mcp_descriptors(self):
        tools, resources, prompts = mcp_support.mcp_descriptors()
        self.assertTrue(any(item.get("kind") == "plugin_surface" for item in tools))
        self.assertTrue(any(item.get("collection") == "routers" for item in tools))
        self.assertTrue(any(item.get("operation") == "poll_now" for item in tools))
        self.assertTrue(
            any(item.get("uri") == "osys://plugin/Keenetic/schema/routers" for item in resources)
        )
        self.assertTrue(any("entity_authoring" in item.get("name", "") for item in prompts))

    def test_mcp_get_prompt_includes_notes(self):
        result = mcp_support.mcp_get_prompt(
            "osys_keenetic_entity_authoring",
            {"task": "add home router", "collection": "routers"},
        )
        text = result["messages"][0]["content"]["text"]
        self.assertIn("Plugin notes:", text)
        self.assertIn("linked_object", text)
        self.assertIn("poll_now", text)

    def test_unsupported_operation(self):
        with self.assertRaises(ValueError):
            mcp_support.mcp_invoke("nope", {})

    def test_poll_now_operation(self):
        try:
            result = mcp_support.mcp_invoke("poll_now", {})
        except ValueError as exc:
            self.assertIn("not loaded", str(exc).lower())
            return
        self.assertTrue(result.get("ok"))
        self.assertEqual(result.get("operation"), "poll_now")


if __name__ == "__main__":
    unittest.main()
