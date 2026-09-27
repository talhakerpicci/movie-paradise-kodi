from resources.lib import identity


def fake_rpc(item, movie=None, show=None, players=None):
    def rpc(method, params):
        if method == "Player.GetActivePlayers":
            return players if players is not None else [{"playerid": 1, "type": "video"}]
        if method == "Player.GetItem":
            return {"item": item}
        if method == "VideoLibrary.GetMovieDetails":
            return {"moviedetails": movie or {}}
        if method == "VideoLibrary.GetTVShowDetails":
            return {"tvshowdetails": show or {}}
        raise AssertionError("unexpected " + method)
    return rpc


def test_a_library_movie():
    item = {"type": "movie", "id": 12, "file": "/media/matrix.mkv", "title": "The Matrix",
            "uniqueid": {"tmdb": "603", "imdb": "tt0133093", "tvdb": ""}}
    found, reason = identity.current(fake_rpc(item))
    assert reason == "" and found.media == {"movie": {"ids": {"tmdb": 603, "imdb": "tt0133093"}}}
    assert found.key == "movie:tmdb:603"


def test_a_movie_falls_back_to_the_library_details():
    item = {"type": "movie", "id": 12, "file": "/m.mkv", "uniqueid": {}}
    found, _ = identity.current(fake_rpc(item, movie={"uniqueid": {}, "imdbnumber": "tt0133093"}))
    assert found.media == {"movie": {"ids": {"imdb": "tt0133093"}}} and found.key == "movie:imdb:tt0133093"


def test_a_library_episode_takes_the_show_ids_and_its_numbers():
    item = {"type": "episode", "id": 44, "tvshowid": 7, "season": 1, "episode": 2, "showtitle": "Game of Thrones",
            "file": "/tv/got/s01e02.mkv", "uniqueid": {"tmdb": "63057", "tvdb": "3436411"}}
    found, reason = identity.current(fake_rpc(item, show={"uniqueid": {"tmdb": "1399", "imdb": "tt0944947"}}))
    assert reason == ""
    assert found.media == {"show": {"ids": {"tmdb": 1399, "imdb": "tt0944947"}}, "episode": {"season": 1, "number": 2}}
    assert found.key == "episode:tmdb:1399:1:2"


def test_season_zero_is_valid():
    item = {"type": "episode", "id": 44, "tvshowid": 7, "season": 0, "episode": 1, "file": "/tv/x.mkv"}
    found, _ = identity.current(fake_rpc(item, show={"uniqueid": {"tmdb": "1399"}}))
    assert found.media["episode"] == {"season": 0, "number": 1}


def test_a_plugin_episode_needs_its_own_imdb_id():
    item = {"type": "episode", "id": -1, "tvshowid": -1, "season": 1, "episode": 2,
            "file": "plugin://plugin.video.someaddon/play", "uniqueid": {"imdb": "tt1480055", "tmdb": "63057"}}
    found, _ = identity.current(fake_rpc(item))
    assert found.media == {"episode": {"ids": {"imdb": "tt1480055"}}}


def test_an_episode_tmdb_id_is_never_read_as_the_show():
    item = {"type": "episode", "id": -1, "tvshowid": -1, "season": 1, "episode": 2,
            "file": "plugin://plugin.video.someaddon/play", "uniqueid": {"tmdb": "63057"}}
    assert identity.current(fake_rpc(item)) == (None, identity.SKIP_NO_IDS)


def test_tvdb_only_is_skipped():
    item = {"type": "movie", "id": 12, "file": "/m.mkv", "uniqueid": {"tvdb": "123"}}
    assert identity.current(fake_rpc(item, movie={"uniqueid": {"tvdb": "123"}})) == (None, identity.SKIP_NO_IDS)


def test_live_tv_stacks_and_other_types_are_skipped():
    assert identity.current(fake_rpc({"type": "channel", "file": "pvr://channels/tv/1"}))[1] == identity.SKIP_LIVE_TV
    assert identity.current(fake_rpc({"type": "movie", "file": "stack://a.mkv , b.mkv"}))[1] == identity.SKIP_STACK
    assert identity.current(fake_rpc({"type": "musicvideo", "file": "/mv.mkv"}))[1] == identity.SKIP_TYPE
    assert identity.current(fake_rpc({"type": "movie"}, players=[{"playerid": 0, "type": "audio"}]))[1] == identity.SKIP_NOT_PLAYING


def test_server_addon_plays_are_skipped_unless_asked():
    item = {"type": "movie", "id": -1, "file": "plugin://plugin.video.jellyfin/?mode=play", "uniqueid": {"tmdb": "603"}}
    assert identity.current(fake_rpc(item))[1] == identity.SKIP_SERVER_ADDON
    found, _ = identity.current(fake_rpc(item), exclude_server_addons=False)
    assert found is not None


def test_malformed_ids_are_ignored():
    item = {"type": "movie", "id": -1, "file": "/m.mkv", "uniqueid": {"tmdb": "abc", "imdb": "nm0000206"}}
    assert identity.current(fake_rpc(item)) == (None, identity.SKIP_NO_IDS)


def test_trailers_and_extras_are_skipped_even_with_the_movies_ids():
    ids = {"tmdb": "603", "imdb": "tt0133093"}
    for path in ("/media/The Matrix/The Matrix-trailer.mp4", "/media/The Matrix/Trailers/teaser.mkv",
                 "/media/The Matrix/extras/making of.mkv", "/media/The Matrix/Behind The Scenes/bts.mkv",
                 "/media/The Matrix/trailer.mkv", "/media/The Matrix/Sample/the.matrix-sample.mkv",
                 "plugin://plugin.video.youtube/play/?video_id=abc"):
        item = {"type": "movie", "id": -1, "file": path, "uniqueid": ids}
        assert identity.current(fake_rpc(item)) == (None, identity.SKIP_EXTRA), path


def test_the_library_movies_own_trailer_is_skipped():
    item = {"type": "movie", "id": 12, "file": "https://example.com/matrix_trailer_1080.mp4", "uniqueid": {"tmdb": "603"}}
    movie = {"uniqueid": {"tmdb": "603"}, "trailer": "https://example.com/matrix_trailer_1080.mp4"}
    assert identity.current(fake_rpc(item, movie=movie)) == (None, identity.SKIP_EXTRA)


def test_film_titles_that_look_like_extras_are_still_shared():
    for path in ("/media/The Big Short (2015)/The.Big.Short.mkv", "/media/The Interview/The.Interview.mkv",
                 "/media/Shorts/Pixar Shorts Collection.mkv", "/media/Deleted/Deleted.2021.mkv",
                 "/media/Other/Other.People.mkv", "/media/Sampled/Sampled.mkv"):
        item = {"type": "movie", "id": -1, "file": path, "uniqueid": {"tmdb": "318846"}}
        found, reason = identity.current(fake_rpc(item))
        assert found is not None, (path, reason)


def test_a_file_far_shorter_than_the_titles_runtime_is_not_the_title():
    item = {"type": "movie", "id": -1, "file": "plugin://plugin.video.someaddon/play?id=9", "runtime": 8160,
            "uniqueid": {"tmdb": "603"}}
    found, _ = identity.current(fake_rpc(item))
    assert found.runtime_ms == 8_160_000
    assert identity.plausible_length(found, 150_000) is False, "a trailer carrying the movie's runtime"
    assert identity.plausible_length(found, 7_800_000) is True, "a shorter cut is still the movie"
    assert identity.plausible_length(found, 9_600_000) is True, "an extended cut too"
    assert identity.plausible_length(found, None) is True and identity.plausible_length(found, 0) is True


def test_without_a_known_runtime_any_length_passes():
    for runtime in (None, 0, -5, "8160", True):
        item = {"type": "movie", "id": -1, "file": "/m/short.film.mkv", "runtime": runtime, "uniqueid": {"tmdb": "1"}}
        found, _ = identity.current(fake_rpc(item))
        assert found.runtime_ms is None and identity.plausible_length(found, 60_000) is True, runtime
