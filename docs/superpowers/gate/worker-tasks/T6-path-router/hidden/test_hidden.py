import pytest

from path_router import MethodNotAllowed, NotFound, Router


def make():
    r = Router()
    r.add("get", "/users/{id:int}", "user_by_id")
    r.add("GET", "/users/{name}/posts", "posts")
    r.add("POST", "/users", "create_user")
    r.add("GET", "/", "root")
    return r


def test_int_and_str_params():
    r = make()
    assert r.match("GET", "/users/42") == ("user_by_id", {"id": 42})
    assert r.match("get", "/users/ann/posts") == ("posts", {"name": "ann"})


def test_int_param_rejects_non_digits():
    r = make()
    with pytest.raises(NotFound):
        r.match("GET", "/users/abc")
    with pytest.raises(NotFound):
        r.match("GET", "/users/-1")


def test_trailing_slash_and_root():
    r = make()
    assert r.match("GET", "/users/7/") == ("user_by_id", {"id": 7})
    assert r.match("GET", "/") == ("root", {})


def test_literal_beats_param_regardless_of_order():
    r = Router()
    r.add("GET", "/users/{id}", "by_id")
    r.add("GET", "/users/me", "me")
    assert r.match("GET", "/users/me") == ("me", {})
    assert r.match("GET", "/users/bob") == ("by_id", {"id": "bob"})


def test_leftmost_literal_wins():
    r = Router()
    r.add("GET", "/{a}/x", "param_first")
    r.add("GET", "/b/{c}", "literal_first")
    assert r.match("GET", "/b/x")[0] == "literal_first"


def test_method_not_allowed_lists_sorted_methods():
    r = make()
    r.add("DELETE", "/users", "drop")
    with pytest.raises(MethodNotAllowed) as e:
        r.match("GET", "/users")
    assert e.value.allowed == ["DELETE", "POST"]


def test_not_found_and_duplicates():
    r = make()
    with pytest.raises(NotFound):
        r.match("GET", "/nope")
    with pytest.raises(ValueError):
        r.add("get", "/users/{id:int}", "again")
    assert issubclass(NotFound, Exception) and issubclass(MethodNotAllowed, Exception)
