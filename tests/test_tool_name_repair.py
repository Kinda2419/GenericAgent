from agent_loop import repair_tool_name


VALID = {
    "code_run",
    "ask_user",
    "web_scan",
    "web_execute_js",
    "file_patch",
    "file_write",
    "file_read",
    "update_working_checkpoint",
    "no_tool",
    "list_dir",
    "start_long_term_update",
}


def test_unique_short_prefix_repairs():
    assert repair_tool_name("li", VALID) == "list_dir"
    assert repair_tool_name("fil", VALID) is None
    assert repair_tool_name("rea", VALID) is None


def test_li_stays_ambiguous_if_more_list_tools_are_added():
    assert repair_tool_name("li", VALID | {"list_items"}) is None


def test_ambiguous_short_prefix_does_not_guess():
    assert repair_tool_name("fi", VALID) is None
    assert repair_tool_name("web", VALID) is None


def test_normalization_repairs_full_name_variants():
    assert repair_tool_name("file-read", VALID) == "file_read"
    assert repair_tool_name("FILE READ", VALID) == "file_read"

