import json
import os
import random
import subprocess
from argparse import Namespace
from pathlib import Path
from typing import Any, cast

from materialyoucolor.hct import Hct
from materialyoucolor.utils.color_utils import argb_from_rgb
from PIL import Image

from caelestia.utils.colourfulness import get_variant
from caelestia.utils.hypr import message
from caelestia.utils.io import warn
from caelestia.utils.material import get_colours_for_image
from caelestia.utils.paths import (
    compute_hash,
    get_config,
    get_wallpaper_engine_assets_dir,
    get_wallpaper_engine_config,
    get_wallpaper_engine_workshop_dir,
    wallpaper_link_path,
    wallpaper_path_path,
    wallpaper_thumbnail_path,
    wallpapers_cache_dir,
)
from caelestia.utils.scheme import Scheme, get_scheme
from caelestia.utils.theme import apply_colours


def is_valid_image(path: Path) -> bool:
    return path.is_file() and path.suffix.lower() in [".jpg", ".jpeg", ".png", ".webp", ".tif", ".tiff", ".gif"]


def is_workshop_dir(path: Path) -> bool:
    return path.is_dir() and (
        (path / "project.json").is_file()
        or (path / "scene.pkg").is_file()
        or any(path.glob("preview.*"))
        or any(path.glob("thumbnail.*"))
    )


def get_workshop_preview(workshop_dir: Path) -> Path | None:
    if not workshop_dir.is_dir():
        return None
    for name in [
        "preview.jpg",
        "preview.png",
        "thumbnail.jpg",
        "thumbnail.png",
        "preview.webp",
        "thumbnail.webp",
        "preview.gif",
        "thumbnail.gif",
    ]:
        p = workshop_dir / name
        if p.is_file():
            return p
    for f in workshop_dir.iterdir():
        if is_valid_image(f):
            return f
    return None


def get_workshop_title(workshop_dir: Path) -> str:
    proj = workshop_dir / "project.json"
    if proj.is_file():
        try:
            data = json.loads(proj.read_text(encoding="utf-8"))
            if title := data.get("title"):
                return str(title).strip()
        except Exception as e:
            warn(f'failed to parse "{proj}": {e}')
    return workshop_dir.name


def stop_linux_wallpaperengine() -> None:
    try:
        subprocess.run(["pkill", "-9", "-f", "linux-wallpaperengine"], stderr=subprocess.DEVNULL)
    except Exception as e:
        warn(f"failed to stop linux-wallpaperengine: {e}")


def apply_linux_wallpaperengine(item_path: Path) -> None:
    stop_linux_wallpaperengine()
    assets_dir = get_wallpaper_engine_assets_dir()
    cmd = ["linux-wallpaperengine", "--silent", "--layer", "background"]
    if assets_dir:
        cmd.extend(["--assets-dir", str(assets_dir)])

    try:
        monitors = cast(list[dict[str, Any]], message("monitors"))
        if monitors:
            for m in monitors:
                if name := m.get("name"):
                    cmd.extend(["--screen-root", str(name), "--scaling", "fill"])
    except Exception as e:
        warn(f"failed to get monitor layout: {e}")

    cmd.append(str(item_path))
    try:
        subprocess.Popen(
            cmd,
            start_new_session=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL,
        )
    except Exception as e:
        warn(f"failed to start linux-wallpaperengine: {e}")


def check_wall(wall: Path, filter_size: tuple[int, int], threshold: float) -> bool:
    with Image.open(wall) as img:
        width, height = img.size
        return width >= filter_size[0] * threshold and height >= filter_size[1] * threshold


def get_wallpaper() -> str | None:
    try:
        return wallpaper_path_path.read_text().strip()
    except IOError:
        return None


def get_wallpapers(args: Namespace) -> list[Path]:
    we_cfg = get_wallpaper_engine_config()
    if we_cfg.get("enabled", False):
        if workshop_dir := get_wallpaper_engine_workshop_dir():
            dirs = [d for d in workshop_dir.iterdir() if is_workshop_dir(d)]
            if dirs:
                return dirs

    directory = Path(args.random)
    if not directory.is_dir():
        return []

    walls = [f for f in directory.rglob("*") if is_valid_image(f)]

    if args.no_filter:
        return walls

    monitors = cast(list[dict[str, int]], message("monitors"))
    filter_size = min(m["width"] for m in monitors), min(m["height"] for m in monitors)

    return [f for f in walls if check_wall(f, filter_size, args.threshold)]


def get_thumb(wall: Path, cache: Path) -> Path:
    thumb = cache / "thumbnail.jpg"

    if not thumb.exists():
        with Image.open(wall) as img:
            img = img.convert("RGB")
            img.thumbnail((128, 128), Image.Resampling.NEAREST)
            thumb.parent.mkdir(parents=True, exist_ok=True)
            img.save(thumb, "JPEG")

    return thumb


def get_smart_opts(wall: Path, cache: Path) -> dict:
    opts_cache = cache / "smart.json"

    try:
        return json.loads(opts_cache.read_text())
    except (IOError, json.JSONDecodeError):
        pass

    opts = {}

    with Image.open(get_thumb(wall, cache)) as img:
        opts["variant"] = get_variant(img)
        img.thumbnail((1, 1), Image.Resampling.LANCZOS)

        # Cast the pixel to a tuple of 3 integers to safely unpack it
        pixel = cast(tuple[int, int, int], img.getpixel((0, 0)))
        hct = Hct.from_int(argb_from_rgb(*pixel))

        opts["mode"] = "light" if hct.tone > 60 else "dark"

    opts_cache.parent.mkdir(parents=True, exist_ok=True)
    with opts_cache.open("w") as f:
        json.dump(opts, f)

    return opts


def convert_gif(wall: Path) -> Path:
    cache = wallpapers_cache_dir / compute_hash(wall)
    output_path = cache / "first_frame.png"

    if not output_path.exists():
        output_path.parent.mkdir(parents=True, exist_ok=True)
        with Image.open(wall) as img:
            n_frames = getattr(img, "n_frames", 1)
            frame_idx = min(n_frames // 2, 5) if n_frames > 1 else 0
            try:
                img.seek(frame_idx)
            except (EOFError, ValueError):
                pass

            img = img.convert("RGB")
            img.save(output_path, "PNG")

    return output_path


def resolve_wallpaper_source(wall: Path | str) -> tuple[Path, Path, Path, bool]:
    wall_path = Path(wall).resolve()
    if is_workshop_dir(wall_path):
        preview = get_workshop_preview(wall_path)
        if not preview:
            raise ValueError(f'"{wall_path}" is a workshop folder but contains no valid preview image')
        theme_image = convert_gif(preview) if preview.suffix.lower() == ".gif" else preview
        return wall_path, preview, theme_image, True

    if not is_valid_image(wall_path):
        raise ValueError(f'"{wall_path}" is not a valid image')

    theme_image = convert_gif(wall_path) if wall_path.suffix.lower() == ".gif" else wall_path
    return wall_path, wall_path, theme_image, False


def get_colours_for_wall(wall: Path | str, no_smart: bool) -> dict | None:
    try:
        _, _, theme_image, _ = resolve_wallpaper_source(wall)
    except ValueError:
        return None

    scheme = get_scheme()
    cache = wallpapers_cache_dir / compute_hash(theme_image)

    name = "dynamic"

    if not no_smart:
        smart_opts = get_smart_opts(theme_image, cache)
        scheme = Scheme(
            {
                "name": name,
                "flavour": scheme.flavour,
                "mode": smart_opts["mode"],
                "variant": smart_opts["variant"],
                "colours": scheme.colours,
            }
        )

    return {
        "name": name,
        "flavour": scheme.flavour,
        "mode": scheme.mode,
        "variant": scheme.variant,
        "colours": get_colours_for_image(get_thumb(theme_image, cache), scheme),
    }


def set_wallpaper(wall: Path | str, no_smart: bool = False) -> None:
    wall_path, preview, theme_image, is_workshop = resolve_wallpaper_source(wall)

    if not is_workshop:
        stop_linux_wallpaperengine()

    # Update files
    wallpaper_path_path.parent.mkdir(parents=True, exist_ok=True)
    wallpaper_path_path.write_text(str(wall_path))
    wallpaper_link_path.parent.mkdir(parents=True, exist_ok=True)
    wallpaper_link_path.unlink(missing_ok=True)
    wallpaper_link_path.symlink_to(theme_image if is_workshop else wall_path)

    cache = wallpapers_cache_dir / compute_hash(theme_image)

    # Generate thumbnail or get from cache
    thumb = get_thumb(theme_image, cache)
    wallpaper_thumbnail_path.parent.mkdir(parents=True, exist_ok=True)
    wallpaper_thumbnail_path.unlink(missing_ok=True)
    wallpaper_thumbnail_path.symlink_to(thumb)

    scheme = get_scheme()

    # Change mode and variant based on wallpaper colour
    if scheme.name == "dynamic" and not no_smart:
        smart_opts = get_smart_opts(theme_image, cache)
        scheme.mode = smart_opts["mode"]
        scheme.variant = smart_opts["variant"]

    # Update colours
    scheme.update_colours()
    apply_colours(scheme.colours, scheme.mode)

    # Run custom post-hook if configured
    cfg = get_config().get("wallpaper", {})
    if post_hook := cfg.get("postHook"):
        subprocess.run(
            post_hook,
            shell=True,
            env={
                **os.environ,
                "WALLPAPER_PATH": str(wall_path),
                "PREVIEW_PATH": str(preview),
                "SCHEME_NAME": scheme.name,
                "SCHEME_FLAVOUR": scheme.flavour,
                "SCHEME_MODE": scheme.mode,
                "SCHEME_VARIANT": scheme.variant,
                "SCHEME_COLOURS": json.dumps(scheme.colours),
                "THUMBNAIL_PATH": str(thumb),
            },
            stderr=subprocess.DEVNULL,
        )

    if is_workshop:
        apply_linux_wallpaperengine(wall_path)


def restore_wallpaper(no_smart: bool = False) -> None:
    current = get_wallpaper()
    if not current:
        return
    p = Path(current)
    if p.exists():
        set_wallpaper(p, no_smart)


def set_random(args: Namespace) -> None:
    wallpapers = get_wallpapers(args)

    if not wallpapers:
        raise ValueError("No valid wallpapers found")

    try:
        last_wall = wallpaper_path_path.read_text()
        wallpapers.remove(Path(last_wall))

        if not wallpapers:
            raise ValueError("Only valid wallpaper is current")
    except (FileNotFoundError, ValueError):
        pass

    set_wallpaper(random.choice(wallpapers), args.no_smart)
