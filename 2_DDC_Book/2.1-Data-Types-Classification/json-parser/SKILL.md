---
name: "json-parser"
description: "Parse and validate JSON data from construction APIs, IoT sensors, and BIM exports. Transform nested JSON to flat DataFrames."
homepage: "https://datadrivenconstruction.io"
metadata: {"openclaw": {"emoji": "🏷️", "os": ["win32"], "homepage": "https://datadrivenconstruction.io", "requires": {"bins": ["python3"]}}}
---
# JSON Parser for Construction Data

## Overview
Construction systems increasingly use JSON for data exchange - from IoT sensors to BIM metadata exports. This skill handles parsing, validation, and flattening of JSON structures.

## Python Implementation

```python
import json
import re
import pandas as pd
from typing import Dict, Any, List, Optional, Union
from dataclasses import dataclass
from pathlib import Path


# Maximum nesting of objects/arrays accepted from a document. Both the
# standard JSON decoder and the flattener would otherwise walk one stack
# frame per level, so a deeply nested payload could crash the import.
MAX_JSON_DEPTH = 32

_JSON_TOKEN = re.compile(r'"(?:[^"\\]|\\.)*"|[\[\]{}]', re.DOTALL)


class JSONDepthError(ValueError):
    """Raised when a document is nested deeper than MAX_JSON_DEPTH."""

    def __init__(self, max_depth: int = MAX_JSON_DEPTH):
        super().__init__(f"JSON nesting depth exceeds the maximum of {max_depth}")


def check_text_depth(text: Union[str, bytes, bytearray],
                     max_depth: int = MAX_JSON_DEPTH) -> None:
    """Refuse raw JSON text nested deeper than max_depth, without decoding it."""
    if isinstance(text, (bytes, bytearray)):
        text = text.decode(json.detect_encoding(text), 'surrogatepass')
    depth = 0
    for match in _JSON_TOKEN.finditer(text):
        token = match.group()
        if token in '[{':
            depth += 1
            if depth > max_depth:
                raise JSONDepthError(max_depth)
        elif token in ']}':
            depth -= 1


def check_data_depth(data: Any, max_depth: int = MAX_JSON_DEPTH) -> None:
    """Refuse decoded data nested deeper than max_depth, without recursing."""
    stack = [(data, 1)]
    while stack:
        node, depth = stack.pop()
        if isinstance(node, dict):
            children = node.values()
        elif isinstance(node, list):
            children = node
        else:
            continue
        if depth > max_depth:
            raise JSONDepthError(max_depth)
        for child in children:
            if isinstance(child, (dict, list)):
                stack.append((child, depth + 1))


@dataclass
class JSONParseResult:
    """Result of JSON parsing operation."""
    success: bool
    data: Any
    errors: List[str]
    record_count: int


class ConstructionJSONParser:
    """Parse JSON data from construction sources."""

    def __init__(self):
        self.errors: List[str] = []

    def parse_file(self, file_path: str) -> JSONParseResult:
        """Parse JSON from file."""
        try:
            with open(file_path, 'r', encoding='utf-8') as f:
                text = f.read()
            check_text_depth(text)
            data = json.loads(text)
            return JSONParseResult(True, data, [], self._count_records(data))
        except JSONDepthError as e:
            return JSONParseResult(False, None, [str(e)], 0)
        except json.JSONDecodeError as e:
            return JSONParseResult(False, None, [f"JSON Error: {e}"], 0)
        except Exception as e:
            return JSONParseResult(False, None, [str(e)], 0)

    def parse_string(self, json_string: str) -> JSONParseResult:
        """Parse JSON from string."""
        try:
            check_text_depth(json_string)
            data = json.loads(json_string)
            return JSONParseResult(True, data, [], self._count_records(data))
        except JSONDepthError as e:
            return JSONParseResult(False, None, [str(e)], 0)
        except json.JSONDecodeError as e:
            return JSONParseResult(False, None, [f"JSON Error: {e}"], 0)

    def _count_records(self, data: Any) -> int:
        """Count records in data."""
        if isinstance(data, list):
            return len(data)
        elif isinstance(data, dict):
            return 1
        return 0

    def flatten_json(self, data: Dict, prefix: str = '') -> Dict[str, Any]:
        """Flatten nested JSON to single-level dict."""
        check_data_depth(data)
        flat = {}
        stack = [(prefix, iter(data.items()), False)]
        while stack:
            parent, items, in_list = stack[-1]
            entry = next(items, None)
            if entry is None:
                stack.pop()
                continue
            key, value = entry

            if in_list:
                item_key = f"{parent}_{key}"
                if isinstance(value, dict):
                    stack.append((item_key, iter(value.items()), False))
                else:
                    flat[item_key] = value
                continue

            new_key = f"{parent}_{key}" if parent else key
            if isinstance(value, dict):
                stack.append((new_key, iter(value.items()), False))
            elif isinstance(value, list):
                if all(isinstance(i, (str, int, float, bool, type(None))) for i in value):
                    flat[new_key] = value
                else:
                    stack.append((new_key, enumerate(value), True))
            else:
                flat[new_key] = value
        return flat

    def to_dataframe(self, data: Union[List[Dict], Dict]) -> pd.DataFrame:
        """Convert JSON data to DataFrame."""
        if isinstance(data, list):
            flat_records = [self.flatten_json(r) if isinstance(r, dict) else {'value': r} for r in data]
            return pd.DataFrame(flat_records)
        elif isinstance(data, dict):
            if all(isinstance(v, list) for v in data.values()):
                # Dict of lists - columnar format
                return pd.DataFrame(data)
            else:
                flat = self.flatten_json(data)
                return pd.DataFrame([flat])
        return pd.DataFrame()

    def extract_elements(self, data: Dict, path: str) -> List[Any]:
        """Extract elements using dot notation path."""
        parts = path.split('.')
        current = data

        for part in parts:
            if isinstance(current, dict) and part in current:
                current = current[part]
            elif isinstance(current, list) and part.isdigit():
                current = current[int(part)]
            else:
                return []

        return current if isinstance(current, list) else [current]

    def validate_schema(self, data: Dict,
                        required_fields: List[str]) -> Dict[str, Any]:
        """Validate JSON against required fields."""
        flat = self.flatten_json(data)
        missing = [f for f in required_fields if f not in flat]
        present = [f for f in required_fields if f in flat]

        return {
            'valid': len(missing) == 0,
            'missing_fields': missing,
            'present_fields': present,
            'completeness': len(present) / len(required_fields) * 100
        }


# BIM JSON Parser
class BIMJSONParser(ConstructionJSONParser):
    """Specialized parser for BIM JSON exports."""

    def parse_bim_elements(self, data: Dict) -> pd.DataFrame:
        """Parse BIM elements from JSON export."""
        elements = []

        # Common BIM JSON structures
        if 'elements' in data:
            elements = data['elements']
        elif 'objects' in data:
            elements = data['objects']
        elif 'entities' in data:
            elements = data['entities']
        elif isinstance(data, list):
            elements = data

        if not elements:
            return pd.DataFrame()

        # Flatten each element
        flat_elements = []
        for elem in elements:
            if isinstance(elem, dict):
                flat = self.flatten_json(elem)
                flat_elements.append(flat)

        return pd.DataFrame(flat_elements)

    def extract_properties(self, element: Dict) -> Dict[str, Any]:
        """Extract properties from BIM element."""
        props = {}

        # Common property locations in BIM JSON
        for key in ['properties', 'params', 'parameters', 'attributes']:
            if key in element and isinstance(element[key], dict):
                props.update(element[key])

        return props


# IoT JSON Parser
class IoTJSONParser(ConstructionJSONParser):
    """Parser for IoT sensor data."""

    def parse_sensor_reading(self, data: Dict) -> Dict[str, Any]:
        """Parse single sensor reading."""
        return {
            'sensor_id': data.get('sensor_id') or data.get('id'),
            'timestamp': data.get('timestamp') or data.get('time'),
            'value': data.get('value') or data.get('reading'),
            'unit': data.get('unit', ''),
            'location': data.get('location', '')
        }

    def parse_sensor_batch(self, data: List[Dict]) -> pd.DataFrame:
        """Parse batch of sensor readings."""
        readings = [self.parse_sensor_reading(r) for r in data]
        return pd.DataFrame(readings)
```

## Quick Start

```python
parser = ConstructionJSONParser()

# Parse from file
result = parser.parse_file("bim_export.json")
if result.success:
    df = parser.to_dataframe(result.data)
    print(f"Loaded {len(df)} records")

# Flatten nested JSON
flat = parser.flatten_json(result.data)

# Extract specific path
elements = parser.extract_elements(result.data, "project.building.floors")
```

## Common Use Cases

### 1. BIM Metadata
```python
bim_parser = BIMJSONParser()
result = bim_parser.parse_file("revit_export.json")
elements = bim_parser.parse_bim_elements(result.data)
```

### 2. IoT Sensors
```python
iot_parser = IoTJSONParser()
readings = iot_parser.parse_sensor_batch(sensor_data)
```

### 3. API Response
```python
parser = ConstructionJSONParser()
result = parser.parse_string(api_response)
df = parser.to_dataframe(result.data)
```

## Resources
- **DDC Book**: Chapter 2.1 - Semi-structured Data
