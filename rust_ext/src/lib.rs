//! Rust extension for Adroid — fast uiautomator XML parser.
//!
//! Performance: ~10x faster than Python's xml.etree.ElementTree for large
//! UI trees (1000+ nodes). Uses quick-xml's streaming API (StAX).

use pyo3::prelude::*;
use pyo3::types::{PyDict, PyList};
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

/// Convert ParsedNode to Python dict.
fn node_to_pydict<'py>(py: Python<'py>, node: &ParsedNode) -> PyResult<Bound<'py, PyDict>> {
    let dict = PyDict::new_bound(py);
    dict.set_item("uuid", &node.uuid)?;
    dict.set_item("role", &node.role)?;
    dict.set_item("text", &node.text)?;
    dict.set_item("description", &node.description)?;
    dict.set_item("resource_id", &node.resource_id)?;
    dict.set_item("class_name", &node.class_name)?;
    dict.set_item("package", &node.package)?;

    let bounds_dict = PyDict::new_bound(py);
    bounds_dict.set_item("x", node.bounds.0)?;
    bounds_dict.set_item("y", node.bounds.1)?;
    bounds_dict.set_item("w", node.bounds.2)?;
    bounds_dict.set_item("h", node.bounds.3)?;
    dict.set_item("bounds", bounds_dict)?;

    dict.set_item("clickable", node.clickable)?;
    dict.set_item("scrollable", node.scrollable)?;
    dict.set_item("editable", node.role == "edit_text" && node.focusable)?;
    dict.set_item("enabled", node.enabled)?;
    dict.set_item("visible", true)?;
    dict.set_item("focused", node.focused)?;
    dict.set_item("selected", node.selected)?;
    dict.set_item("parent_uuid", &node.parent_uuid)?;
    dict.set_item("children_uuids", node.children_uuids.clone())?;
    dict.set_item("confidence", 1.0f64)?;
    dict.set_item("source", "a11y")?;
    Ok(dict)
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
    Ok("0.3.5".to_string())
}

/// Python module definition.
#[pymodule]
fn adroid_rust(_py: Python, m: &Bound<PyModule>) -> PyResult<()> {
    m.add_function(wrap_pyfunction!(parse_uiautomator_xml_json, m)?)?;
    m.add_function(wrap_pyfunction!(parse_bounds_py, m)?)?;
    m.add_function(wrap_pyfunction!(infer_role_py, m)?)?;
    m.add_function(wrap_pyfunction!(version, m)?)?;
    m.add("__version__", "0.3.5")?;
    Ok(())
}
