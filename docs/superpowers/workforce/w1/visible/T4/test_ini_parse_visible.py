from ini_parse import get_int, parse


def test_a_section_with_keys():
    cfg = parse("[server]\nhost = example.org\nport = 8080\n")
    assert cfg["server"]["host"] == "example.org"
    assert get_int(cfg, "server", "port") == 8080
