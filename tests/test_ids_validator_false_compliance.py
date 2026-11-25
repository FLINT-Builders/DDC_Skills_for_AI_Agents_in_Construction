"""IDSValidator must fail a model that misses a real IDS requirement.

The class under test is the IDSValidator code block in
2_DDC_Book/4.3-BIM-Validation-Pipeline/bim-validation-pipeline/SKILL.md.
These tests load that block and run IDSValidator.validate. They fail while
the skill parses IDS with an un-namespaced ElementTree search and copies
only XML attributes.

buildingSMART IDS 1.0 (ids.xsd targetNamespace
http://standards.buildingsmart.org/IDS, elementFormDefault=qualified) puts
every element in that namespace. Entity name, propertySet, and baseName are
child idsValue/simpleValue elements, not attributes. A clean validate()
result is what an operator reads as compliance, so these tests require the
missing property to be reported and require a document that yields no
usable specification or facet name to raise.

The IFC class string passed to model.by_type is the entity simpleValue
verbatim (the shipped checker does not rename it). Property sets are the
mapping element_util.get_psets already returns to _check_requirements.
"""

from __future__ import annotations

import re
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace


REPO = Path(__file__).resolve().parents[1]
SKILL = (
    REPO
    / "2_DDC_Book"
    / "4.3-BIM-Validation-Pipeline"
    / "bim-validation-pipeline"
    / "SKILL.md"
)
OFFICIAL_IDS = (
    Path(__file__).resolve().parent
    / "fixtures"
    / "fail-elements_with_no_properties_always_fail.ids"
)
IDS_NS = "http://standards.buildingsmart.org/IDS"

# Not a parse error: these mean the snippet failed to load, not that an
# IDS document was rejected.
_LOAD_ERRORS = (NameError, AttributeError, ImportError, SyntaxError)


def _element_util():
    def get_psets(element):
        return element.psets

    return SimpleNamespace(get_psets=get_psets)


def load_ids_validator():
    text = SKILL.read_text(encoding="utf-8")
    blocks = re.findall(r"```python\n(.*?)```", text, flags=re.DOTALL)
    block = next(b for b in blocks if "class IDSValidator" in b)
    namespace = {"element_util": _element_util()}
    exec(compile(block, str(SKILL), "exec"), namespace)
    return namespace["IDSValidator"]


class IfcElement:
    def __init__(self, guid, ifc_class, psets=None):
        self.GlobalId = guid
        self.ifc_class = ifc_class
        self.psets = {} if psets is None else psets


class IfcModel:
    """by_type plus the pset dicts _check_requirements already indexes."""

    def __init__(self, elements):
        self._elements = list(elements)
        self.requested_types = []

    def by_type(self, ifc_type):
        self.requested_types.append(ifc_type)
        return [e for e in self._elements if e.ifc_class == ifc_type]


def _write(xml: str) -> str:
    handle = tempfile.NamedTemporaryFile(
        mode="w", suffix=".ids", delete=False, encoding="utf-8"
    )
    handle.write(xml)
    handle.close()
    return handle.name


def _issues_by_element(results):
    grouped = {}
    for item in results:
        grouped.setdefault(item["element"], [])
        issues = item["issues"]
        if isinstance(issues, str):
            grouped[item["element"]].append(issues)
        else:
            grouped[item["element"]].extend(issues)
    return grouped


def _ids_document(body: str, prefixed: bool = False) -> str:
    if prefixed:
        return (
            '<?xml version="1.0" encoding="UTF-8"?>\n'
            f'<ids:ids xmlns:ids="{IDS_NS}" '
            'xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" '
            f'xsi:schemaLocation="{IDS_NS} '
            'http://standards.buildingsmart.org/IDS/1.0/ids.xsd">\n'
            f"{body}\n</ids:ids>\n"
        )
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        f'<ids xmlns="{IDS_NS}" '
        'xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" '
        f'xsi:schemaLocation="{IDS_NS} '
        'http://standards.buildingsmart.org/IDS/1.0/ids.xsd">\n'
        f"{body}\n</ids>\n"
    )


def _spec(name, ifc_class, properties):
    """One specification. properties is a list of (propertySet, baseName)."""
    props = []
    for pset, base in properties:
        props.append(
            "        <property>\n"
            "          <propertySet><simpleValue>"
            f"{pset}</simpleValue></propertySet>\n"
            "          <baseName><simpleValue>"
            f"{base}</simpleValue></baseName>\n"
            "        </property>\n"
        )
    return (
        f'    <specification name="{name}" ifcVersion="IFC4">\n'
        "      <applicability minOccurs=\"1\" maxOccurs=\"unbounded\">\n"
        "        <entity>\n"
        "          <name><simpleValue>"
        f"{ifc_class}</simpleValue></name>\n"
        "        </entity>\n"
        "      </applicability>\n"
        "      <requirements>\n"
        f"{''.join(props)}"
        "      </requirements>\n"
        "    </specification>\n"
    )


def _prefix_ids(xml: str) -> str:
    """Rewrite a default-namespace IDS document to an ids: prefixed one."""
    xml = xml.replace(f'xmlns="{IDS_NS}"', f'xmlns:ids="{IDS_NS}"', 1)
    xml = re.sub(r"<(/?)([A-Za-z][\w.-]*)", r"<\1ids:\2", xml)
    xml = xml.replace("<ids:?xml", "<?xml")
    xml = xml.replace("ids:xmlns:ids", "xmlns:ids")
    xml = xml.replace("ids:xmlns:xsi", "xmlns:xsi")
    xml = xml.replace("ids:xsi:", "xsi:")
    return xml


class IdsFalseComplianceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.IDSValidator = load_ids_validator()

    def _model(self):
        return IfcModel(
            [
                IfcElement("WALL_MISSING", "IFCWALL", {}),
                IfcElement(
                    "WALL_OK",
                    "IFCWALL",
                    {"Foo_Bar": {"Foo": "present"}},
                ),
                IfcElement("SLAB_OTHER", "IFCSLAB", {}),
            ]
        )

    def _assert_only_missing_wall(self, results, model, message):
        self.assertTrue(
            results,
            f"{message}: validate returned {results!r}; "
            f"by_type requests were {model.requested_types!r}",
        )
        grouped = _issues_by_element(results)
        self.assertIn("WALL_MISSING", grouped, grouped)
        self.assertIn("Missing property: Foo_Bar.Foo", grouped["WALL_MISSING"])
        self.assertNotIn("WALL_OK", grouped, grouped)
        self.assertNotIn("SLAB_OTHER", grouped, grouped)

    def _assert_parse_error(self, path, model):
        try:
            validator = self.IDSValidator(path)
        except _LOAD_ERRORS:
            raise
        except Exception:
            return
        try:
            results = validator.validate(model)
        except _LOAD_ERRORS:
            raise
        except Exception:
            return
        self.fail(
            "a document with no usable IDS specification or facet name "
            f"was treated as compliance: {results!r}"
        )

    def test_official_ids_file_reports_missing_property(self):
        """Unmodified buildingSMART IDS 0.9/1.0 sample, default namespace.

        Fixture: tests/fixtures/fail-elements_with_no_properties_always_fail.ids
        copied from buildingSMART/IDS v1.0.0
        Documentation/testcases/property/fail-elements_with_no_properties_always_fail.ids.
        Applicability entity simpleValue is IFCWALL. The required property
        is propertySet Foo_Bar, baseName Foo, stored as child elements.
        The property element only carries a dataType attribute.
        """
        model = self._model()
        validator = self.IDSValidator(str(OFFICIAL_IDS))
        results = validator.validate(model)
        self._assert_only_missing_wall(
            results,
            model,
            "official namespaced IDS against a wall with no properties",
        )

    def test_prefixed_ids_namespace_reports_missing_property(self):
        """Same requirements with an explicit ids: prefix on every element."""
        body = (
            "  <info><title>Wall property</title></info>\n"
            "  <specifications>\n"
            + _spec("Wall property", "IFCWALL", [("Foo_Bar", "Foo")])
            + "  </specifications>\n"
        )
        path = _write(_prefix_ids(_ids_document(body)))
        model = self._model()
        validator = self.IDSValidator(path)
        results = validator.validate(model)
        self._assert_only_missing_wall(
            results, model, "prefixed IDS namespace"
        )

    def test_child_elements_without_a_namespace_are_not_attributes(self):
        """Namespace-free IDS still stores names in child elements.

        Copying only XML attributes leaves constraints without name or
        propertySet, so no element is checked.
        """
        xml = """<?xml version="1.0" encoding="UTF-8"?>
<ids>
  <specifications>
    <specification name="Wall property" ifcVersion="IFC4">
      <applicability>
        <entity>
          <name><simpleValue>IFCWALL</simpleValue></name>
        </entity>
      </applicability>
      <requirements>
        <property dataType="IFCLABEL">
          <propertySet><simpleValue>Foo_Bar</simpleValue></propertySet>
          <baseName><simpleValue>Foo</simpleValue></baseName>
        </property>
      </requirements>
    </specification>
  </specifications>
</ids>
"""
        model = self._model()
        validator = self.IDSValidator(_write(xml))
        results = validator.validate(model)
        self._assert_only_missing_wall(
            results, model, "un-namespaced child elements"
        )

    def test_every_specification_and_property_is_enforced(self):
        """Two specifications and two property facets must all be applied.

        A parser that keeps only the first specification, or only an
        attribute on the property element, still returns a clean result
        for the requirement it dropped.
        """
        body = (
            "  <info><title>Two specs</title></info>\n"
            "  <specifications>\n"
            + _spec(
                "Wall properties",
                "IFCWALL",
                [("Pset_WallCommon", "FireRating"), ("Pset_WallCommon", "Combustible")],
            )
            + _spec(
                "Slab load bearing",
                "IFCSLAB",
                [("Pset_SlabCommon", "LoadBearing")],
            )
            + "  </specifications>\n"
        )
        model = IfcModel(
            [
                IfcElement("WALL_MISSING", "IFCWALL", {}),
                IfcElement(
                    "WALL_OK",
                    "IFCWALL",
                    {
                        "Pset_WallCommon": {
                            "FireRating": "2HR",
                            "Combustible": False,
                        }
                    },
                ),
                IfcElement("SLAB_MISSING", "IFCSLAB", {}),
                IfcElement(
                    "SLAB_OK",
                    "IFCSLAB",
                    {"Pset_SlabCommon": {"LoadBearing": True}},
                ),
            ]
        )
        validator = self.IDSValidator(_write(_ids_document(body)))
        results = validator.validate(model)
        self.assertTrue(
            results,
            f"multi-spec IDS returned {results!r}; "
            f"by_type requests were {model.requested_types!r}",
        )
        grouped = _issues_by_element(results)
        self.assertIn("WALL_MISSING", grouped, grouped)
        self.assertIn(
            "Missing property: Pset_WallCommon.FireRating",
            grouped["WALL_MISSING"],
        )
        self.assertIn(
            "Missing property: Pset_WallCommon.Combustible",
            grouped["WALL_MISSING"],
        )
        self.assertIn("SLAB_MISSING", grouped, grouped)
        self.assertIn(
            "Missing property: Pset_SlabCommon.LoadBearing",
            grouped["SLAB_MISSING"],
        )
        self.assertNotIn("WALL_OK", grouped, grouped)
        self.assertNotIn("SLAB_OK", grouped, grouped)

    def test_zero_specifications_is_a_parse_error(self):
        """An IDS document that yields no specification is not a pass."""
        xml = _ids_document(
            "  <info><title>Empty</title></info>\n  <specifications/>\n"
        )
        self._assert_parse_error(_write(xml), self._model())

    def test_facet_without_a_name_is_a_parse_error(self):
        """Whitespace entity name, or a property with no baseName, is not a pass.

        Skipping that facet selects no elements or checks no property, and
        validate returns an empty list for a wall that was never judged.
        """
        blank_entity = _ids_document(
            "  <info><title>Blank entity</title></info>\n"
            "  <specifications>\n"
            "    <specification name=\"Blank entity\" ifcVersion=\"IFC4\">\n"
            "      <applicability>\n"
            "        <entity>\n"
            "          <name><simpleValue>   </simpleValue></name>\n"
            "        </entity>\n"
            "      </applicability>\n"
            "      <requirements>\n"
            "        <property>\n"
            "          <propertySet><simpleValue>Pset_WallCommon</simpleValue></propertySet>\n"
            "          <baseName><simpleValue>FireRating</simpleValue></baseName>\n"
            "        </property>\n"
            "      </requirements>\n"
            "    </specification>\n"
            "  </specifications>\n"
        )
        blank_property = _ids_document(
            "  <info><title>Blank property</title></info>\n"
            "  <specifications>\n"
            "    <specification name=\"Blank property\" ifcVersion=\"IFC4\">\n"
            "      <applicability>\n"
            "        <entity>\n"
            "          <name><simpleValue>IFCWALL</simpleValue></name>\n"
            "        </entity>\n"
            "      </applicability>\n"
            "      <requirements>\n"
            "        <property dataType=\"IFCLABEL\">\n"
            "          <propertySet><simpleValue>Pset_WallCommon</simpleValue></propertySet>\n"
            "          <baseName><simpleValue></simpleValue></baseName>\n"
            "        </property>\n"
            "      </requirements>\n"
            "    </specification>\n"
            "  </specifications>\n"
        )
        model = self._model()
        self._assert_parse_error(_write(blank_entity), model)
        self._assert_parse_error(_write(blank_property), model)


if __name__ == "__main__":
    unittest.main()
