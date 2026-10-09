from path_router import Router


def test_literal_and_capture():
    r = Router()
    r.add("get", "/users/{id:int}", "show")
    assert r.match("GET", "/users/42") == ("show", {"id": 42})
