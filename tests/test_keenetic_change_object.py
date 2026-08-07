"""Keenetic changeObject binding tests."""
from tests import change_object_harness as _coh
from plugins.Keenetic.models.Device import KeeneticDevice
from plugins.Keenetic.models.Router import Router


class TestKeeneticChangeObject(_coh.ChangeObjectSqliteTestCase):
    MODEL_MODULES = (
        "plugins.Keenetic.models.Device",
        "plugins.Keenetic.models.Router",
        "plugins.Keenetic.models.Vpn",
        "plugins.Keenetic.models.LogRule",
    )
    SESSION_MODULE = "plugins.Keenetic"
    PLUGIN_MODULE = "plugins.Keenetic"
    PLUGIN_CLASS = "Keenetic"
    PLUGIN_NAME = "Keenetic"
    EXTRA_PLUGIN_ATTRS = {"_load_entity_cache": lambda: None}

    def _call(self, *args):
        plugin = self.make_plugin()
        with self.patch_session_scope():
            plugin.changeObject(*args)

    def test_property_delete_keeps_device_link(self):
        router = Router(title="R1", ip="1.1.1.1")
        self.db.session.add(router)
        self.db.session.flush()
        self.db.session.add(
            KeeneticDevice(router_id=router.id, mac="aa:bb", title="Phone", linked_object="Alice")
        )
        self.db.session.commit()
        self._call("delete", "Alice", "online", None, None)
        self.assertEqual(KeeneticDevice.query.first().linked_object, "Alice")

    def test_object_rename_updates_device_link(self):
        router = Router(title="R1", ip="1.1.1.1")
        self.db.session.add(router)
        self.db.session.flush()
        self.db.session.add(
            KeeneticDevice(router_id=router.id, mac="aa:bb", title="Phone", linked_object="Alice")
        )
        self.db.session.commit()
        self._call("rename", "Alice", None, None, "Alice2")
        self.assertEqual(KeeneticDevice.query.first().linked_object, "Alice2")

    def test_object_delete_clears_router_method_binding(self):
        self.db.session.add(
            Router(title="R1", ip="1.1.1.1", linked_object="Alice", linked_method="onEvent")
        )
        self.db.session.commit()
        self._call("delete", "Alice", None, None, None)
        router = Router.query.first()
        self.assertIsNone(router.linked_object)
        self.assertIsNone(router.linked_method)

    def test_method_rename_updates_router_linked_method(self):
        self.db.session.add(
            Router(title="R1", ip="1.1.1.1", linked_object="Alice", linked_method="oldMethod")
        )
        self.db.session.commit()
        self._call("rename", "Alice", None, "oldMethod", "newMethod")
        self.assertEqual(Router.query.first().linked_method, "newMethod")
