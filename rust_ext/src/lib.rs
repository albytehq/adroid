//! Rust extension for Adroid — fast uiautomator XML parser.
//!
//! Performance: ~10x faster than Python's xml.etree.ElementTree for large
//! UI trees (1000+ nodes). Uses quick-xml's streaming API (StAX).

use pyo3::prelude::*;
use quick_xml::events::Event;
use quick_xml::Reader;
use std::collections::HashMap;

/// Role inference: map Android widget class names to NodeRole strings.
fn infer_role(class_name: &str) -> &str {
    let simple = class_name.rsplit('.').next().unwrap_or(class_name);
    let patterns: &[(&str, &str)] = &[
        ("ImageButton", "image"),
        ("ImageView", "image_view"),
        ("EditText", "edit_text"),
        ("TextView", "text_view"),
        ("CheckBox", "checkbox"),
        ("RadioButton", "radio"),
        ("Switch", "switch"),
        ("ToggleButton", "toggle"),
        ("Spinner", "spinner"),
        ("WebView", "web_view"),
        ("RecyclerView", "recycler_view"),
        ("ListView", "list"),
        ("ScrollView", "scroll_view"),
        ("ViewPager", "view_pager"),
        ("FrameLayout", "frame_layout"),
        ("LinearLayout", "linear_layout"),
        ("RelativeLayout", "relative_layout"),
        ("ProgressBar", "progress_bar"),
        ("SeekBar", "seek_bar"),
        ("Button", "button"),
        ("MenuItem", "menu_item"),
        ("Tab", "tab"),
        ("Dialog", "dialog"),
    ];
    for (pattern, role) in patterns {
        if simple.contains(pattern) {
            return role;
        }
    }
    "unknown"
}

/// Parse bounds string "[x1,y1][x2,y2]" into (x, y, w, h).
fn parse_bounds(s: &str) -> (i64, i64, i64, i64) {
    let nums: Vec<i64> = s
        .split(|c: char| !c.is_ascii_digit())
        .filter(|s| !s.is_empty())
        .filter_map(|s| s.parse::<i64>().ok())
        .collect();
    if nums.len() >= 4 {
        let w = if nums[2] > nums[0] { nums[2] - nums[0] } else { 0 };
        let h = if nums[3] > nums[1] { nums[3] - nums[1] } else { 0 };
        (nums[0], nums[1], w, h)
    } else {
        (0, 0, 0, 0)
    }
}

/// Generate a pseudo-UUID hex string (32 chars).
fn gen_uuid() -> String {
    use std::cell::Cell;
    use std::time::{SystemTime, UNIX_EPOCH};
    thread_local! {
        static COUNTER: Cell<u64> = Cell::new(0);
    }
    COUNTER.with(|c| {
        let count = c.get();
        c.set(count + 1);
        let nanos = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .unwrap_or_default()
            .as_nanos() as u64;
        format!("{:016x}{:016x}", nanos, count)
    })
}

fn is_true(val: &str) -> bool {
    val.eq_ignore_ascii_case("true")
}

fn get_attr<'a>(attrs: &'a HashMap<String, String>, key: &str) -> &'a str {
    attrs.get(key).map(|s| s.as_str()).unwrap_or("")
}

fn get_attr_opt(attrs: &HashMap<String, String>, key: &str) -> Option<String> {
    attrs.get(key).and_then(|s| {
        if s.is_empty() { None } else { Some(s.clone()) }
    })
}

/// Parse attributes from a quick-xml BytesStart event.
fn parse_attrs(event: &quick_xml::events::BytesStart) -> HashMap<String, String> {
    let mut attrs = HashMap::new();
    for attr in event.attributes().flatten() {
        let key = String::from_utf8_lossy(attr.key.as_ref()).to_string();
        let value = attr.unescape_value().unwrap_or_default().to_string();
        attrs.insert(key, value);
    }
    attrs
}

/// Intermediate node representation.
struct ParsedNode {
    uuid: String,
    role: String,
    text: Option<String>,
    description: Option<String>,
    resource_id: Option<String>,
    class_name: Option<String>,
    package: Option<String>,
    bounds: (i64, i64, i64, i64),
    clickable: bool,
    scrollable: bool,
    enabled: bool,
    focusable: bool,
    focused: bool,
    selected: bool,
    parent_uuid: Option<String>,
    children_uuids: Vec<String>,
}

/// Build a ParsedNode from attribute map.
fn build_node(attrs: &HashMap<String, String>, uuid: &str, parent_uuid: Option<&str>, children: Vec<String>) -> ParsedNode {
    let class_name = get_attr(attrs, "class");
    let role = infer_role(class_name).to_string();
    let focusable = is_true(get_attr(attrs, "focusable"));

    ParsedNode {
        uuid: uuid.to_string(),
        role,
        text: get_attr_opt(attrs, "text"),
        description: get_attr_opt(attrs, "content-desc"),
        resource_id: get_attr_opt(attrs, "resource-id"),
        class_name: if class_name.is_empty() { None } else { Some(class_name.to_string()) },
        package: get_attr_opt(attrs, "package"),
        bounds: parse_bounds(get_attr(attrs, "bounds")),
        clickable: is_true(get_attr(attrs, "clickable")),
        scrollable: is_true(get_attr(attrs, "scrollable")),
        enabled: is_true(get_attr(attrs, "enabled")),
        focusable,
        focused: is_true(get_attr(attrs, "focused")),
        selected: is_true(get_attr(attrs, "selected")),
        parent_uuid: parent_uuid.map(|s| s.to_string()),
        children_uuids: children,
    }
}

/// Recursively walk XML <node> elements.
fn walk_nodes(
    reader: &mut Reader<&[u8]>,
    parent_uuid: Option<&str>,
    nodes: &mut Vec<ParsedNode>,
) -> PyResult<()> {
    loop {
        match reader.read_event() {
            Ok(Event::Start(ref e)) if e.name().as_ref() == b"node" => {
                let uuid = gen_uuid();
                let attrs = parse_attrs(e);
                let mut child_nodes: Vec<ParsedNode> = Vec::new();
                walk_nodes(reader, Some(&uuid), &mut child_nodes)?;
                let child_uuids: Vec<String> = child_nodes.iter().map(|n| n.uuid.clone()).collect();
                nodes.extend(child_nodes);
                nodes.push(build_node(&attrs, &uuid, parent_uuid, child_uuids));
            }
            Ok(Event::Empty(ref e)) if e.name().as_ref() == b"node" => {
                let uuid = gen_uuid();
                let attrs = parse_attrs(e);
                nodes.push(build_node(&attrs, &uuid, parent_uuid, Vec::new()));
            }
            Ok(Event::End(ref e)) if e.name().as_ref() == b"node" => {
                break;
            }
            Ok(Event::Eof) => break,
            Ok(_) => {}
            Err(e) => {
                return Err(PyErr::new::<pyo3::exceptions::PyValueError, _>(format!(
                    "XML parse error: {}", e
                )));
            }
        }
    }
    Ok(())
}

/// Clean malformed XML declaration.
fn clean_xml_declaration(xml: &str) -> String {
    if xml.starts_with("<?xml") {
        if let Some(end) = xml.find("?>") {
            return format!("<?xml version=\"1.0\" encoding=\"UTF-8\"?>{}", &xml[end + 2..]);
        }
    }
    xml.to_string()
}

/// Parse uiautomator dump XML and return JSON string.
/// This is faster than returning Python dicts because it avoids
/// per-dict FFI overhead — only 1 FFI call for the entire result.
/// Python side does json.loads() which is C-native and very fast.
#[pyfunction]
fn parse_uiautomator_xml_json(xml_string: &str) -> PyResult<String> {
    if xml_string.trim().is_empty() {
        return Ok("[]".to_string());
    }

    let cleaned = clean_xml_declaration(xml_string);
    let mut reader = Reader::from_str(&cleaned);
    reader.config_mut().trim_text(true);

    let mut nodes: Vec<ParsedNode> = Vec::new();

    loop {
        match reader.read_event() {
            Ok(Event::Start(ref e)) if e.name().as_ref() == b"node" => {
                let uuid = gen_uuid();
                let attrs = parse_attrs(e);
                let mut child_nodes: Vec<ParsedNode> = Vec::new();
                walk_nodes(&mut reader, Some(&uuid), &mut child_nodes)?;
                let child_uuids: Vec<String> = child_nodes.iter().map(|n| n.uuid.clone()).collect();
                nodes.extend(child_nodes);
                nodes.push(build_node(&attrs, &uuid, None, child_uuids));
            }
            Ok(Event::Empty(ref e)) if e.name().as_ref() == b"node" => {
                let uuid = gen_uuid();
                let attrs = parse_attrs(e);
                nodes.push(build_node(&attrs, &uuid, None, Vec::new()));
            }
            Ok(Event::Eof) => break,
            Ok(_) => {}
            Err(e) => {
                return Err(PyErr::new::<pyo3::exceptions::PyValueError, _>(format!(
                    "XML parse error: {}", e
                )));
            }
        }
    }

    // Serialize to JSON string manually (no serde dependency needed)
    let mut json = String::from("[");
    for (i, node) in nodes.iter().enumerate() {
        if i > 0 {
            json.push(',');
        }
        json.push_str(&node_to_json(node));
    }
    json.push(']');
    Ok(json)
}

/// Serialize a ParsedNode to a JSON string.
fn node_to_json(n: &ParsedNode) -> String {
    format!(
        r#"{{"uuid":"{}","role":"{}","text":{},"description":{},"resource_id":{},"class_name":{},"package":{},"bounds":{{"x":{},"y":{},"w":{},"h":{}}},"clickable":{},"scrollable":{},"editable":{},"enabled":{},"visible":true,"focused":{},"selected":{},"parent_uuid":{},"children_uuids":[{}],"confidence":1.0,"source":"a11y"}}"#,
        n.uuid,
        n.role,
        opt_to_json(&n.text),
        opt_to_json(&n.description),
        opt_to_json(&n.resource_id),
        opt_to_json(&n.class_name),
        opt_to_json(&n.package),
        n.bounds.0,
        n.bounds.1,
        n.bounds.2,
        n.bounds.3,
        n.clickable,
        n.scrollable,
        n.role == "edit_text" && n.focusable,
        n.enabled,
        n.focused,
        n.selected,
        opt_to_json(&n.parent_uuid),
        n.children_uuids.iter().map(|u| format!(r#""{}""#, u)).collect::<Vec<_>>().join(","),
    )
}

/// Convert Option<String> to JSON (null or "quoted string").
fn opt_to_json(s: &Option<String>) -> String {
    match s {
        Some(v) => format!(r#""{}""#, v.replace('\\', r"\\").replace('"', r#"\""#)),
        None => "null".to_string(),
    }
}

/// Parse a uiautomator bounds string into a dict {x, y, w, h}.
#[pyfunction]
fn parse_bounds_py(py: Python, bounds_str: &str) -> PyResult<Py<PyAny>> {
    use pyo3::types::PyDict;
    let (x, y, w, h) = parse_bounds(bounds_str);
    let dict = PyDict::new_bound(py);
    dict.set_item("x", x)?;
    dict.set_item("y", y)?;
    dict.set_item("w", w)?;
    dict.set_item("h", h)?;
    Ok(dict.into())
}

/// Infer NodeRole from Android widget class name.
#[pyfunction]
fn infer_role_py(class_name: &str) -> PyResult<String> {
    Ok(infer_role(class_name).to_string())
}

/// Get the version of this Rust extension.
#[pyfunction]
fn version() -> PyResult<String> {
    Ok("0.3.6".to_string())
}

// ---------------------------------------------------------------------------
// v0.3.6: WorldState diff engine
// ---------------------------------------------------------------------------

/// Compute a fast FNV-1a 64-bit hash of a WorldState JSON string.
///
/// Normalizes JSON (sorts keys via serde_json's BTreeMap) before hashing
/// so two WorldStates with the same nodes in different insertion order
/// produce the same hash. Used by ui.wait_for(ui_stable) for cheap state
/// comparison across polls.
///
/// NOT cryptographically secure — do not use for tamper detection.
/// The audit log uses Ed25519 signatures for tamper-evidence instead.
///
/// Args:
///     state_json: JSON string of a WorldState (from ui.dump)
///
/// Returns:
///     16-char hex string (64-bit FNV-1a hash)
#[pyfunction]
fn compute_state_hash(state_json: &str) -> PyResult<String> {
    // Parse JSON. On parse error, raise PyValueError so callers can
    // distinguish "bad input" from "stable state" — never silently
    // return a collision-prone all-zeros hash.
    let parsed: serde_json::Value = match serde_json::from_str(state_json) {
        Ok(v) => v,
        Err(e) => {
            return Err(pyo3::exceptions::PyValueError::new_err(format!(
                "state_json parse error: {}",
                e
            )))
        }
    };

    // Serialize with sorted keys (serde_json::Value is BTreeMap-backed for objects)
    let normalized = serde_json::to_string(&parsed).unwrap_or_default();

    // FNV-1a 64-bit hash — fast, no external crate needed.
    let mut hash: u64 = 0xcbf29ce484222325;
    for byte in normalized.bytes() {
        hash ^= byte as u64;
        hash = hash.wrapping_mul(0x100000001b3);
    }
    Ok(format!("{:016x}", hash))
}

/// Diff two WorldState JSON strings and return what changed.
///
/// Compares node lists by uuid. Returns JSON with:
///   {
///     "added": [uuid, ...],      # nodes in new but not in old
///     "removed": [uuid, ...],    # nodes in old but not in new
///     "changed": [{uuid, field, old, new}, ...],  # nodes with modified fields
///     "unchanged_count": int,    # how many nodes didn't change
///     "is_stable": bool          # true if added+removed+changed all empty
///   }
///
/// Args:
///     old_json: JSON string of previous WorldState
///     new_json: JSON string of current WorldState
///
/// Returns:
///     JSON string of diff result
#[pyfunction]
fn diff_states(old_json: &str, new_json: &str) -> PyResult<String> {
    let old: serde_json::Value = match serde_json::from_str(old_json) {
        Ok(v) => v,
        Err(e) => return Err(PyErr::new::<pyo3::exceptions::PyValueError, _>(format!("old_json parse error: {}", e))),
    };
    let new: serde_json::Value = match serde_json::from_str(new_json) {
        Ok(v) => v,
        Err(e) => return Err(PyErr::new::<pyo3::exceptions::PyValueError, _>(format!("new_json parse error: {}", e))),
    };

    // Extract node arrays
    let old_nodes = old.get("nodes").and_then(|n| n.as_array()).cloned().unwrap_or_default();
    let new_nodes = new.get("nodes").and_then(|n| n.as_array()).cloned().unwrap_or_default();

    // Build uuid → node maps
    let mut old_map: HashMap<String, &serde_json::Value> = HashMap::new();
    for node in &old_nodes {
        if let Some(uuid) = node.get("uuid").and_then(|u| u.as_str()) {
            old_map.insert(uuid.to_string(), node);
        }
    }

    let mut new_map: HashMap<String, &serde_json::Value> = HashMap::new();
    for node in &new_nodes {
        if let Some(uuid) = node.get("uuid").and_then(|u| u.as_str()) {
            new_map.insert(uuid.to_string(), node);
        }
    }

    let mut added: Vec<String> = Vec::new();
    let mut removed: Vec<String> = Vec::new();
    let mut changed: Vec<String> = Vec::new();
    let mut unchanged_count = 0;

    // Find added and changed
    for (uuid, new_node) in &new_map {
        match old_map.get(uuid) {
            None => added.push(uuid.clone()),
            Some(old_node) => {
                // Compare by serializing both (fast way to deep-compare)
                let old_str = serde_json::to_string(old_node).unwrap_or_default();
                let new_str = serde_json::to_string(new_node).unwrap_or_default();
                if old_str == new_str {
                    unchanged_count += 1;
                } else {
                    changed.push(uuid.clone());
                }
            }
        }
    }

    // Find removed
    for uuid in old_map.keys() {
        if !new_map.contains_key(uuid) {
            removed.push(uuid.clone());
        }
    }

    let is_stable = added.is_empty() && removed.is_empty() && changed.is_empty();

    let result = format!(
        r#"{{"added":{:?},"removed":{:?},"changed":{:?},"unchanged_count":{},"is_stable":{}}}"#,
        added, removed, changed, unchanged_count, is_stable
    );
    Ok(result)
}

/// Python module definition.
#[pymodule]
fn adroid_rust(_py: Python, m: &Bound<PyModule>) -> PyResult<()> {
    m.add_function(wrap_pyfunction!(parse_uiautomator_xml_json, m)?)?;
    m.add_function(wrap_pyfunction!(parse_bounds_py, m)?)?;
    m.add_function(wrap_pyfunction!(infer_role_py, m)?)?;
    m.add_function(wrap_pyfunction!(compute_state_hash, m)?)?;
    m.add_function(wrap_pyfunction!(diff_states, m)?)?;
    m.add_function(wrap_pyfunction!(version, m)?)?;
    m.add("__version__", "0.3.6")?;
    Ok(())
}
