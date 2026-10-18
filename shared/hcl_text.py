"""Text and HCL helpers for generate_abac.py's generated tfvars.

String-level HCL rendering, top-level block and list removal, model-draft
sanitizing and syntax repair. Split out of generate_abac.py without change
(18.7): generate_abac.py re-exports every name, and
scripts/check_pure_move.py proves each body is byte-identical to its original.
"""

import re
from pathlib import Path


def _hcl_str(s: str) -> str:
    """Format a Python string as an HCL quoted string literal."""
    escaped = s.replace("\\", "\\\\").replace('"', '\\"').replace("${", "$${")
    return f'"{escaped}"'


def format_genie_space_configs_hcl(configs: dict[str, dict]) -> str:
    """Convert a dict of {space_key: config_dict} to the genie_space_configs HCL block.

    The space_key is the human-readable name used as the map key in abac.auto.tfvars.
    """
    lines = ["genie_space_configs = {"]

    for space_name, cfg in configs.items():
        lines.append(f"  {_hcl_str(space_name)} = {{")

        if cfg.get("title"):
            lines.append(f"    title = {_hcl_str(cfg['title'])}")

        if cfg.get("description"):
            lines.append(f"    description = {_hcl_str(cfg['description'])}")

        if cfg.get("instructions"):
            lines.append(f"    instructions = {_hcl_str(cfg['instructions'])}")

        if cfg.get("sample_questions"):
            lines.append("    sample_questions = [")
            for q in cfg["sample_questions"]:
                lines.append(f"      {_hcl_str(q)},")
            lines.append("    ]")

        if cfg.get("benchmarks"):
            lines.append("    benchmarks = [")
            for bm in cfg["benchmarks"]:
                if isinstance(bm, dict) and "question" in bm and "sql" in bm:
                    lines.append("      {")
                    lines.append(f"        question = {_hcl_str(bm['question'])}")
                    lines.append(f"        sql      = {_hcl_str(bm['sql'])}")
                    lines.append("      },")
                # Skip malformed benchmarks (e.g. plain strings from LLM)
            lines.append("    ]")

        if cfg.get("sql_filters"):
            lines.append("    sql_filters = [")
            for f in cfg["sql_filters"]:
                if isinstance(f, dict) and "sql" in f:
                    lines.append("      {")
                    lines.append(f"        sql          = {_hcl_str(f['sql'])}")
                    lines.append(f"        display_name = {_hcl_str(f.get('display_name', ''))}")
                    lines.append(f"        comment      = {_hcl_str(f.get('comment', ''))}")
                    lines.append(f"        instruction  = {_hcl_str(f.get('instruction', ''))}")
                    lines.append("      },")
                # Skip malformed filters (e.g. plain strings from LLM)
            lines.append("    ]")

        if cfg.get("sql_expressions"):
            lines.append("    sql_expressions = [")
            for e in cfg["sql_expressions"]:
                if isinstance(e, dict) and "alias" in e and "sql" in e:
                    lines.append("      {")
                    lines.append(f"        alias        = {_hcl_str(e['alias'])}")
                    lines.append(f"        sql          = {_hcl_str(e['sql'])}")
                    lines.append(f"        display_name = {_hcl_str(e.get('display_name', ''))}")
                    lines.append(f"        comment      = {_hcl_str(e.get('comment', ''))}")
                    lines.append(f"        instruction  = {_hcl_str(e.get('instruction', ''))}")
                    lines.append("      },")
                # Skip malformed expressions (e.g. plain strings from LLM)
            lines.append("    ]")

        if cfg.get("sql_measures"):
            lines.append("    sql_measures = [")
            for m in cfg["sql_measures"]:
                if isinstance(m, dict) and "alias" in m and "sql" in m:
                    lines.append("      {")
                    lines.append(f"        alias        = {_hcl_str(m['alias'])}")
                    lines.append(f"        sql          = {_hcl_str(m['sql'])}")
                    lines.append(f"        display_name = {_hcl_str(m.get('display_name', ''))}")
                    lines.append(f"        comment      = {_hcl_str(m.get('comment', ''))}")
                    lines.append(f"        instruction  = {_hcl_str(m.get('instruction', ''))}")
                    lines.append("      },")
                # Skip malformed measures (e.g. plain strings from LLM)
            lines.append("    ]")

        if cfg.get("join_specs"):
            lines.append("    join_specs = [")
            for j in cfg["join_specs"]:
                if isinstance(j, dict) and "left_table" in j and "right_table" in j and "sql" in j:
                    lines.append("      {")
                    lines.append(f"        left_table   = {_hcl_str(j['left_table'])}")
                    lines.append(f"        right_table  = {_hcl_str(j['right_table'])}")
                    lines.append(f"        sql          = {_hcl_str(j['sql'])}")
                    lines.append(f"        comment      = {_hcl_str(j.get('comment', ''))}")
                    lines.append(f"        instruction  = {_hcl_str(j.get('instruction', ''))}")
                    lines.append(f"        left_alias   = {_hcl_str(j.get('left_alias', ''))}")
                    lines.append(f"        right_alias  = {_hcl_str(j.get('right_alias', ''))}")
                    lines.append("      },")
                # Skip malformed join_specs (e.g. plain strings from LLM)
            lines.append("    ]")

        lines.append("  }")

    lines.append("}")
    return "\n".join(lines)


_REVIEWED_GENIE_FIELDS = (
    "benchmarks", "sql_filters", "sql_expressions", "sql_measures", "join_specs",
)


def _hcl_value_end(text: str, start: int) -> int:
    """End of one quoted or balanced HCL value (comments/strings preserved)."""
    opener = text[start]
    if opener not in "[{\"":
        return text.find("\n", start) if "\n" in text[start:] else len(text)
    closer = {
        "[": "]", "{": "}", '"': '"',
    }[opener]
    top_quote = opener == '"'
    depth, escaped, quoted = 0, False, top_quote
    for index in range(start, len(text)):
        char = text[index]
        if quoted:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"' and index > start:
                if top_quote:
                    return index + 1
                quoted = False
            continue
        if char == '"':
            quoted = True
        elif char == opener:
            depth += 1
        elif char == closer:
            depth -= 1
            if depth == 0:
                return index + 1
    return len(text)


def _space_field_span(text: str, space_name: str, field: str) -> tuple[int, int] | None:
    space = re.search(rf"(?m)^  {re.escape(_hcl_str(space_name))}\s*=\s*\{{", text)
    if not space:
        return None
    space_end = _hcl_value_end(text, space.end() - 1)
    match = re.search(rf"(?m)^    {re.escape(field)}\s*=\s*", text[space.start():space_end])
    if not match:
        return None
    start = space.start() + match.start()
    value_start = space.start() + match.end()
    end = _hcl_value_end(text, value_start)
    return start, end


def restore_reviewed_genie_text(existing: str, rendered: str, spaces: list[str]) -> str:
    """Restore reviewed fields verbatim after rendering refreshed API config."""
    for name in spaces:
        for field in _REVIEWED_GENIE_FIELDS:
            old_span = _space_field_span(existing, name, field)
            new_span = _space_field_span(rendered, name, field)
            if old_span and new_span:
                old = existing[old_span[0]:old_span[1]]
                rendered = rendered[:new_span[0]] + old + rendered[new_span[1]:]
    return rendered


def format_string_map_hcl(name: str, values: dict[str, str]) -> str:
    """Render a deterministic tool-owned string map."""
    lines = [f"{name} = {{"]
    for key in sorted(values):
        lines.append(f"  {_hcl_str(key)} = {_hcl_str(values[key])}")
    lines.append("}")
    return "\n".join(lines)


def remove_hcl_top_level_block(text: str, key: str) -> str:
    """Remove a top-level HCL assignment block 'key = { ... }' from text.

    Uses brace counting to correctly handle nested objects.
    """
    import re as _re

    pattern = _re.compile(rf"^{_re.escape(key)}\s*=\s*\{{", _re.MULTILINE)
    m = pattern.search(text)
    if not m:
        return text

    start = m.start()
    depth = 0
    end = m.end() - 1  # position of the opening {

    for i in range(m.end() - 1, len(text)):
        if text[i] == "{":
            depth += 1
        elif text[i] == "}":
            depth -= 1
            if depth == 0:
                end = i
                break

    # Strip one surrounding newline to avoid double blank lines
    block_end = end + 1
    if block_end < len(text) and text[block_end] == "\n":
        block_end += 1

    return text[:start] + text[block_end:]


def remove_hcl_top_level_list(text: str, key: str) -> str:
    """Remove a top-level HCL assignment list 'key = [ ... ]' from text.

    Uses bracket counting to correctly handle nested structures.
    """
    import re as _re

    pattern = _re.compile(rf"^{_re.escape(key)}\s*=\s*\[", _re.MULTILINE)
    m = pattern.search(text)
    if not m:
        return text

    start = m.start()
    depth = 0
    end = m.end() - 1  # position of the opening [

    for i in range(m.end() - 1, len(text)):
        if text[i] == "[":
            depth += 1
        elif text[i] == "]":
            depth -= 1
            if depth == 0:
                end = i
                break

    block_end = end + 1
    if block_end < len(text) and text[block_end] == "\n":
        block_end += 1

    return text[:start] + text[block_end:]


def strip_multi_space_legacy_genie_keys(text: str) -> str:
    """Remove flat legacy Genie fields when env.auto.tfvars owns multi-space input."""
    for key in (
        "genie_benchmarks", "genie_sql_filters", "genie_sql_expressions",
        "genie_sql_measures", "genie_join_specs",
    ):
        text = remove_hcl_top_level_block(text, key)
    for key in ("genie_sample_questions", "genie_acl_groups"):
        text = remove_hcl_top_level_list(text, key)
    scalar_re = re.compile(
        r'^\s*(?:genie_space_title|genie_space_description|genie_instructions)\s*=\s*"[^"\n]*"\s*$',
        re.MULTILINE,
    )
    return scalar_re.sub("", text)


def strip_draft_genie_acl_fields(tfvars_path: Path) -> int:
    """Remove model/API ACL fields from multi-space semantic draft entries."""
    import hcl2

    text = tfvars_path.read_text()
    try:
        cfg = hcl2.loads(text)
    except Exception:
        return 0
    configs = cfg.get("genie_space_configs") or {}
    if isinstance(configs, list):
        configs = configs[0] if configs else {}
    if not isinstance(configs, dict):
        return 0
    cleaned: dict[str, dict] = {}
    removed = 0
    for name, value in configs.items():
        if isinstance(value, list):
            value = value[0] if value else {}
        if not isinstance(value, dict):
            cleaned[name] = value
            continue
        entry = dict(value)
        for key in ("acl_groups", "genie_acl_groups"):
            if key in entry:
                entry.pop(key)
                removed += 1
        cleaned[name] = entry
    if removed:
        text = remove_hcl_top_level_block(text, "genie_space_configs").rstrip()
        tfvars_path.write_text(text + "\n\n" + format_genie_space_configs_hcl(cleaned) + "\n")
    return removed


def strip_abac_for_genie_mode(text: str) -> str:
    """Keep only workspace/Genie configuration in self-service Genie mode."""
    for key in ("groups", "group_members"):
        text = remove_hcl_top_level_block(text, key)
    for key in ("tag_policies", "tag_assignments", "fgac_policies"):
        text = remove_hcl_top_level_list(text, key)
    return text


TFVARS_STRIP_KEYS = {
    "databricks_account_id",
    "databricks_client_id",
    "databricks_client_secret",
    "databricks_workspace_id",
    "databricks_workspace_host",
    "governance_mode",
    "uc_catalog_name",
    "uc_schema_name",
    "uc_tables",
}


def sanitize_tfvars_hcl(hcl_block: str) -> str:
    """
    Make AI-generated tfvars easier and safer to use:
    - Strip auth variables (these come from auth.auto.tfvars)
    - Insert section-level explanations and doc links
    """

    # --- Strip auth fields (and common adjacent headers) ---
    stripped_lines: list[str] = []
    for line in hcl_block.splitlines():
        if re.match(r"^\s*#\s*Authentication\b", line, re.IGNORECASE):
            continue
        if re.match(r"^\s*#\s*Databricks\s+Authentication\b", line, re.IGNORECASE):
            continue
        # Strip SQL-style comments that the LLM sometimes emits into HCL output
        if re.match(r"^\s*--", line):
            continue

        m = re.match(r"^\s*([A-Za-z0-9_]+)\s*=", line)
        if m and m.group(1) in TFVARS_STRIP_KEYS:
            continue

        stripped_lines.append(line)

    # Collapse excessive blank lines
    compact: list[str] = []
    last_blank = False
    for line in stripped_lines:
        blank = line.strip() == ""
        if blank and last_blank:
            continue
        compact.append(line)
        last_blank = blank

    text = "\n".join(compact).strip() + "\n"

    # --- Insert explanatory blocks before major sections ---
    docs = (
        "# Docs:\n"
        "# - Governed tags / tag policies: https://docs.databricks.com/en/database-objects/tags.html\n"
        "# - Unity Catalog ABAC overview: https://docs.databricks.com/aws/en/data-governance/unity-catalog/abac\n"
        "# - ABAC policies (masks + filters): https://docs.databricks.com/aws/en/data-governance/unity-catalog/abac/policies\n"
        "# - Row filters + column masks: https://docs.databricks.com/en/tables/row-and-column-filters.html\n"
        "#\n"
    )

    groups_block = (
        "# ----------------------------------------------------------------------------\n"
        "# Groups (business roles)\n"
        "# ----------------------------------------------------------------------------\n"
        "# Keys are group names. Use these to represent business personas (e.g., Analyst,\n"
        "# Researcher, Compliance). These groups are used for workspace onboarding,\n"
        "# Databricks One consumer access, data grants, and optional Genie agent ACLs.\n"
        "#\n"
        + docs
    )

    tag_policies_block = (
        "# ----------------------------------------------------------------------------\n"
        "# Tag policies (governed tags)\n"
        "# ----------------------------------------------------------------------------\n"
        "# Each entry defines a governed tag key and the allowed values. You’ll assign\n"
        "# these tags to tables/columns below, then reference them in FGAC policies.\n"
        "#\n"
        + docs
    )

    tag_assignments_block = (
        "# ----------------------------------------------------------------------------\n"
        "# Tag assignments (classify tables/columns)\n"
        "# ----------------------------------------------------------------------------\n"
        "# Apply governed tags to Unity Catalog objects.\n"
        "# - entity_type: \"tables\" or \"columns\"\n"
        "# - entity_name: fully qualified three-level name\n"
        "#   - table:  \"catalog.schema.Table\"\n"
        "#   - column: \"catalog.schema.Table.Column\"\n"
        "# - Table-level tags are optional; use them to scope column masks or row filters\n"
        "#   to specific tables, or for governance.\n"
        "#\n"
        + docs
    )

    fgac_block = (
        "# ----------------------------------------------------------------------------\n"
        "# FGAC policies (who sees what, and how)\n"
        "# ----------------------------------------------------------------------------\n"
        "# Each entry creates either a COLUMN MASK or ROW FILTER policy.\n"
        "#\n"
        "# Common fields:\n"
        "# - name: logical name for the policy (must be unique)\n"
        "# - policy_type: POLICY_TYPE_COLUMN_MASK | POLICY_TYPE_ROW_FILTER\n"
        "# - catalog: catalog this policy is scoped to\n"
        "# - function_catalog: catalog where the masking UDF lives\n"
        "# - function_schema: schema where the masking UDF lives\n"
        "# - to_principals: list of group names who receive this policy\n"
        "# - except_principals: optional list of groups excluded (break-glass/admin)\n"
        "# - comment: human-readable intent (recommended)\n"
        "#\n"
        "# For COLUMN MASK:\n"
        "# - match_condition: ABAC condition, e.g. hasTagValue('phi_level','full_phi')\n"
        "# - match_alias: the column alias used by the ABAC engine\n"
        "# - function_name: masking UDF name (relative; Terraform prefixes catalog.schema)\n"
        "# - when_condition: (optional) scope to specific tagged tables\n"
        "#\n"
        "# For ROW FILTER:\n"
        "# - when_condition: (optional) scope to specific tagged tables\n"
        "# - function_name: row filter UDF name (relative; must be zero-argument)\n"
        "#\n"
        "# Example \u2014 column mask (mask SSN for analysts, exempt compliance):\n"
        "#   {\n"
        "#     name              = \"mask_ssn_analysts\"\n"
        "#     policy_type       = \"POLICY_TYPE_COLUMN_MASK\"\n"
        "#     to_principals     = [\"Junior_Analyst\", \"Senior_Analyst\"]\n"
        "#     except_principals = [\"Compliance_Officer\"]\n"
        "#     comment           = \"Mask SSN showing only last 4 digits\"\n"
        "#     match_condition   = \"hasTagValue('pii_level', 'highly_sensitive')\"\n"
        "#     match_alias       = \"masked_ssn\"\n"
        "#     function_name     = \"mask_ssn\"\n"
        "#   }\n"
        "#\n"
        "# Example \u2014 row filter (restrict regional staff to their rows):\n"
        "#   {\n"
        "#     name           = \"filter_us_region\"\n"
        "#     policy_type    = \"POLICY_TYPE_ROW_FILTER\"\n"
        "#     to_principals  = [\"US_Region_Staff\"]\n"
        "#     comment        = \"Only show rows where region = US\"\n"
        "#     when_condition = \"hasTagValue('region_scope', 'global')\"\n"
        "#     function_name  = \"filter_by_region_us\"\n"
        "#   }\n"
        "#\n"
        + docs
    )

    def insert_before(pattern: str, block: str, s: str) -> str:
        # Avoid double-inserting if the block already exists nearby
        if block.strip() in s:
            return s
        return re.sub(pattern, block + r"\g<0>", s, count=1, flags=re.MULTILINE)

    genie_configs_block = (
        "# ----------------------------------------------------------------------------\n"
        "# Genie agent configs (per-space semantic configuration)\n"
        "# ----------------------------------------------------------------------------\n"
        "# Each key is the human-readable space name matching genie_spaces[*].name in\n"
        "# env.auto.tfvars. Contains instructions, benchmarks, and SQL measures.\n"
        "#\n"
        "# ACL ownership: set optional acl_groups on the matching genie_spaces[] entry\n"
        "# in env.auto.tfvars. Explicit [] means nobody; omission derives from policies.\n"
        "# Any ACL fields in this generated draft are ignored.\n"
        "#\n"
        + docs
    )

    text = insert_before(r"^groups\s*=\s*\{", groups_block, text)
    text = insert_before(r"^tag_policies\s*=\s*\[", tag_policies_block, text)
    text = insert_before(r"^tag_assignments\s*=\s*\[", tag_assignments_block, text)
    text = insert_before(r"^fgac_policies\s*=\s*\[", fgac_block, text)
    text = insert_before(r"^genie_space_configs\s*=\s*\{", genie_configs_block, text)

    return text


def _trim_incomplete_genie_tail(hcl_block: str) -> tuple[str, int]:
    """Trim a truncated legacy Genie tail when the LLM cuts off mid-section.

    Overlay responses sometimes end partway through `genie_sample_questions`,
    `genie_benchmarks`, or similar legacy single-space keys. When that happens,
    the ABAC sections above are still useful, but the trailing incomplete Genie
    fragment makes the whole tfvars file unparsable. If we detect one of these
    trailing keys without a balanced closing structure, trim from that key onward.
    """
    trailing_keys = (
        "genie_space_title",
        "genie_space_description",
        "genie_sample_questions",
        "genie_instructions",
        "genie_benchmarks",
        "genie_sql_filters",
        "genie_sql_expressions",
        "genie_sql_measures",
        "genie_join_specs",
        "genie_acl_groups",
        "genie_space_configs",
    )
    pattern = re.compile(
        rf"(?m)^\s*(?:{'|'.join(re.escape(k) for k in trailing_keys)})\s*="
    )
    matches = list(pattern.finditer(hcl_block))
    if not matches:
        return hcl_block, 0

    last = matches[-1]
    prefix = hcl_block[:last.start()]
    suffix = hcl_block[last.start():]
    opens = suffix.count("[") + suffix.count("{")
    closes = suffix.count("]") + suffix.count("}")
    if opens <= closes:
        return hcl_block, 0
    trimmed = prefix.rstrip() + "\n"
    return trimmed, 1


def _cleanup_stray_commas(text: str) -> str:
    """Remove stray commas left behind by block removals in HCL text.

    Handles bare comma lines, consecutive commas, and trailing commas before ]
    (but preserves valid trailing commas after ``}`` or quoted strings).
    """
    text = re.sub(r'^\s*,\s*$', '', text, flags=re.MULTILINE)
    text = re.sub(r',(\s*,)+', ',', text)
    # Only strip a comma before ] when preceded by whitespace (bare/stray comma),
    # not when preceded by } or " (valid HCL trailing comma).
    text = re.sub(r'(?<![}"\']),(\s*\])', r'\1', text)
    return text


def fix_hcl_syntax(tfvars_path: Path) -> int:
    """Repair common HCL syntax errors introduced by the LLM.

    1. Missing commas between consecutive objects in a list.  The LLM
       sometimes omits the trailing comma after a closing ``}`` before the
       next ``{``.  Blank lines and comment lines between the two braces
       are handled correctly.
    2. Object-style tag_policy values (``values = [{name="v"}]`` →
       ``values = ["v"]``) — the LLM sometimes copies Terraform resource
       syntax into the plain-string values list of the ABAC config.

    Returns the number of repairs made.
    """
    text = tfvars_path.read_text()
    original = text
    repairs = 0

    # ------------------------------------------------------------------
    # Fix 1: missing commas between adjacent objects in a list.
    # Strategy: scan line by line.  When we find a line that ends with
    # just "}" (possibly indented) and the NEXT non-blank, non-comment
    # line starts with the same or less indentation and a "{", we add a
    # trailing comma to the "}" line.
    # ------------------------------------------------------------------
    lines = text.splitlines(keepends=True)
    out_lines: list[str] = []
    i = 0
    while i < len(lines):
        line = lines[i]
        stripped = line.rstrip('\n').rstrip()
        # Check if this line ends with an un-trailed closing brace
        if stripped.endswith('}') and not stripped.endswith('},'):
            # Look ahead: find the next non-blank, non-comment line
            j = i + 1
            while j < len(lines) and (
                lines[j].strip() == '' or lines[j].lstrip().startswith('#') or lines[j].lstrip().startswith('--')
            ):
                j += 1
            if j < len(lines):
                next_stripped = lines[j].lstrip()
                if next_stripped.startswith('{'):
                    # Add the missing comma
                    line = line.rstrip('\n').rstrip() + ',\n'
                    repairs += 1
        out_lines.append(line)
        i += 1

    text = ''.join(out_lines)

    # ------------------------------------------------------------------
    # Fix 2: convert values = [{name = "v"}, ...] → values = ["v", ...]
    # ------------------------------------------------------------------
    def _object_vals_to_strings(m: re.Match) -> str:
        full = m.group(0)
        names = re.findall(r'name\s*=\s*"([^"]+)"', full)
        if names:
            return 'values = [' + ', '.join(f'"{n}"' for n in names) + ']'
        return full

    fixed2 = re.sub(
        r'values\s*=\s*\[\s*\{[^]]*?\}\s*(?:,\s*\{[^]]*?\}\s*)*\]',
        _object_vals_to_strings,
        text,
        flags=re.DOTALL,
    )
    if fixed2 != text:
        repairs += 1
        text = fixed2

    # ------------------------------------------------------------------
    # Fix 3: strip Terraform variable references ("${var.*}") from tfvars.
    # The LLM sometimes emits e.g. benchmarks = "${var.genie_benchmarks}"
    # which is illegal in .tfvars files.  Replace with an empty list.
    # ------------------------------------------------------------------
    fixed3 = re.sub(
        r'(\b(?:genie_)?(?:benchmarks|sql_filters|sql_expressions|sql_measures|join_specs)\s*=\s*)"?\$\$?\{var\.[^}]+\}"?',
        r'\1[]',
        text,
    )
    if fixed3 != text:
        repairs += 1
        text = fixed3

    # ------------------------------------------------------------------
    # Fix 4: remove LLM placeholder ellipses that cause HCL parse errors.
    #   - Standalone "..." lines
    #   - "[...]" placeholder lists → "[]"
    # ------------------------------------------------------------------
    fixed4 = re.sub(r'^\s*\.\.\..*$\n?', '', text, flags=re.MULTILINE)
    fixed4 = re.sub(r'\[\s*\.\.\.\s*\]', '[]', fixed4)
    if fixed4 != text:
        repairs += 1
        text = fixed4

    # ------------------------------------------------------------------
    # Fix 5: remove stray commas left by autofix block removals.
    # ------------------------------------------------------------------
    fixed5 = _cleanup_stray_commas(text)
    if fixed5 != text:
        repairs += 1
        text = fixed5

    if text != original:
        tfvars_path.write_text(text)
        print(f"  [AUTOFIX] Repaired {repairs} HCL syntax issue(s)")

    return repairs


def _find_bracket_section(text: str, section_name: str) -> tuple[int, int] | None:
    """Find the content range of ``section_name = [ ... ]`` using bracket-depth
    counting so that ``]`` inside quoted strings or nested structures is ignored.

    Returns (start, end) offsets of the *content* between the opening ``[`` and
    the matching closing ``]``, or None if the section is not found.
    """
    pattern = re.compile(rf"\b{re.escape(section_name)}\s*=\s*\[")
    m = pattern.search(text)
    if not m:
        return None
    depth = 1
    in_string = False
    escape_next = False
    content_start = m.end()
    for i in range(content_start, len(text)):
        ch = text[i]
        if escape_next:
            escape_next = False
            continue
        if ch == "\\":
            escape_next = True
            continue
        if ch == '"':
            in_string = not in_string
            continue
        if in_string:
            continue
        if ch == "[":
            depth += 1
        elif ch == "]":
            depth -= 1
            if depth == 0:
                return (content_start, i)
    return None


def _find_brace_blocks(text: str) -> list[tuple[int, int]]:
    """Return (start, end) ranges for each top-level ``{ ... }`` block in *text*.

    Uses bracket-depth counting and respects quoted strings so that ``}``
    inside string values does not terminate the block prematurely.
    """
    blocks: list[tuple[int, int]] = []
    i = 0
    while i < len(text):
        if text[i] == "{":
            depth = 1
            in_string = False
            escape_next = False
            start = i
            j = i + 1
            while j < len(text) and depth > 0:
                ch = text[j]
                if escape_next:
                    escape_next = False
                    j += 1
                    continue
                if ch == "\\":
                    escape_next = True
                    j += 1
                    continue
                if ch == '"':
                    in_string = not in_string
                elif not in_string:
                    if ch == "{":
                        depth += 1
                    elif ch == "}":
                        depth -= 1
                j += 1
            if depth == 0:
                blocks.append((start, j - 1))  # j-1 points to closing }
                i = j
                continue
        i += 1
    return blocks


def _replace_bracket_section(text: str, section_name: str, new_items: list[str]) -> str:
    section = _find_bracket_section(text, section_name)
    if section is None:
        return text
    sec_start, sec_end = section
    if new_items:
        new_section = "\n" + ",\n".join(new_items) + "\n"
    else:
        new_section = "\n"
    return text[:sec_start] + new_section + text[sec_end:]


def _render_tag_policy_block(policy: dict) -> str:
    lines = ["  {"]
    lines.append(f'    key = "{policy.get("key", "")}"')
    description = policy.get("description", "")
    if description:
        lines.append(f'    description = "{description}"')
    values = policy.get("values", []) or []
    rendered_values = "[" + ", ".join(f'"{v}"' for v in values) + "]"
    lines.append(f"    values = {rendered_values}")
    lines.append("  }")
    return "\n".join(lines)


def _render_tag_assignment_block(assignment: dict) -> str:
    lines = ["  {"]
    ordered_keys = ["entity_type", "entity_name", "tag_key", "tag_value"]
    for key in ordered_keys:
        value = assignment.get(key, "")
        lines.append(f'    {key} = "{value}"')
    lines.append("  }")
    return "\n".join(lines)


def _render_treatment_override_block(override: dict) -> str:
    return "\n".join([
        "  {",
        f'    entity_name = "{override.get("entity_name", "")}"',
        f'    treatment   = "{override.get("treatment", "")}"',
        "  }",
    ])


def _render_fgac_policy_block(policy: dict) -> str:
    lines = ["  {"]
    ordered_keys = [
        "name",
        "policy_type",
        "catalog",
        "to_principals",
        "except_principals",
        "comment",
        "match_condition",
        "when_condition",
        "match_alias",
        "function_name",
        "function_catalog",
        "function_schema",
    ]
    for key in ordered_keys:
        value = policy.get(key)
        if value in (None, "", []):
            continue
        if isinstance(value, list):
            rendered = "[" + ", ".join(f'"{v}"' for v in value) + "]"
        else:
            rendered = f'"{value}"'
        lines.append(f"    {key:<16} = {rendered}")
    lines.append("  }")
    return "\n".join(lines)
