#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""在 macOS runner 上用原生 codesign 重签，再打 ipa。"""
import os, sys, glob, plistlib, subprocess, shutil

WORK = os.environ.get("WORK_DIR", "work")
PROF_DIR = os.environ.get("PROF_DIR", "profiles")
IDENT = os.environ.get("SIGN_IDENTITY", "Apple Distribution")
BUILD_NUM = os.environ.get("BUILD_NUM", "27")


def log(*a):
    print("[resign]", *a, flush=True)


def run(cmd, check=True):
    log("$", " ".join(cmd))
    p = subprocess.run(cmd, capture_output=True, text=True)
    if p.returncode != 0 and check:
        log("FAILED rc=", p.returncode)
        log(p.stdout[-3000:])
        log(p.stderr[-3000:])
        raise SystemExit(1)
    return p


def load_profiles():
    out = {}
    for p in glob.glob(os.path.join(PROF_DIR, "*.mobileprovision")):
        raw = open(p, "rb").read()
        i = raw.find(b"<?xml")
        j = raw.rfind(b"</plist>")
        d = plistlib.loads(raw[i:j + 8])
        ent = d.get("Entitlements") or {}
        bid = ent.get("application-identifier", "")
        # 去掉 team 前缀
        if "." in bid:
            bid = bid.split(".", 1)[1]
        out[bid] = {"path": p, "ent": ent, "plist": d}
    log("profiles:", {k: os.path.basename(v["path"]) for k, v in out.items()})
    return out


def write_ent(ent, dst):
    with open(dst, "wb") as f:
        plistlib.dump(ent, f)
    return dst


def set_build(path):
    p = path
    if not os.path.exists(p):
        return
    with open(p, "rb") as f:
        d = plistlib.load(f)
    d["CFBundleVersion"] = BUILD_NUM
    st = os.stat(p)
    with open(p, "wb") as f:
        plistlib.dump(d, f)
    os.chmod(p, st.st_mode)



MAGICS = {b"\xfe\xed\xfa\xce", b"\xfe\xed\xfa\xcf", b"\xce\xfa\xed\xfe",
          b"\xcf\xfa\xed\xfe", b"\xca\xfe\xba\xbe", b"\xbe\xba\xfe\xca"}


def machos(root):
    out = []
    for dp, _, fs in os.walk(root):
        for f in fs:
            p = os.path.join(dp, f)
            try:
                with open(p, "rb") as fh:
                    m = fh.read(4)
            except Exception:
                continue
            if m in MAGICS:
                out.append(p)
    return out


def collect(app):
    """收集所有需要签名的对象：dylib 文件、framework/appex/app 目录。"""
    items = []
    for root, dirs, files in os.walk(app):
        for d in list(dirs):
            full = os.path.join(root, d)
            if d.endswith(".framework") or d.endswith(".appex"):
                items.append(full)
        for f in files:
            if f.endswith(".dylib"):
                items.append(os.path.join(root, f))
    return items


def main():
    payload = os.path.join(WORK, "Payload")
    apps = [d for d in glob.glob(os.path.join(payload, "*.app")) if os.path.isdir(d)]
    if not apps:
        log("no .app found")
        raise SystemExit(1)
    app = apps[0]
    log("app:", app)

    profiles = load_profiles()
    main_bid = plistlib.load(open(os.path.join(app, "Info.plist"), "rb"))["CFBundleIdentifier"]
    log("main bundle id:", main_bid)

    # 1) 主 app 与扩展的 Info.plist 统一构建号
    set_build(os.path.join(app, "Info.plist"))
    for ax in glob.glob(os.path.join(app, "PlugIns/*.appex")):
        set_build(os.path.join(ax, "Info.plist"))

    # 2) 嵌描述文件
    if main_bid not in profiles:
        log("missing profile for", main_bid)
        raise SystemExit(1)
    shutil.copyfile(profiles[main_bid]["path"], os.path.join(app, "embedded.mobileprovision"))
    for ax in glob.glob(os.path.join(app, "PlugIns/*.appex")):
        bid = plistlib.load(open(os.path.join(ax, "Info.plist"), "rb"))["CFBundleIdentifier"]
        if bid in profiles:
            shutil.copyfile(profiles[bid]["path"], os.path.join(ax, "embedded.mobileprovision"))
            log("profile ->", bid)
        else:
            log("!! no profile for", bid)

    # 3) 生成 entitlements 文件
    ent_dir = "ent_gen"
    os.makedirs(ent_dir, exist_ok=True)
    ent_files = {}
    for bid, p in profiles.items():
        ent_files[bid] = write_ent(p["ent"], os.path.join(ent_dir, bid + ".plist"))
    empty = write_ent({}, os.path.join(ent_dir, "empty.plist"))

    # 4) 由内到外签名
    # 3.5) 清掉旧签名（带尾部填充的二进制必须先清，否则 codesign 内部报错）
    olds = machos(app)
    log("mach-o files:", len(olds))
    for p in olds:
        try:
            os.chmod(p, os.stat(p).st_mode | 0o111)
        except Exception as e:
            log("chmod fail", p, e)
        run(["codesign", "--remove-signature", p], check=False)

    # 删掉残留的旧签名目录，避免 codesign 内部报错
    removed = 0
    for dp, dirs, fs in os.walk(app):
        if "_CodeSignature" in dirs:
            shutil.rmtree(os.path.join(dp, "_CodeSignature"), ignore_errors=True)
            removed += 1
            dirs.remove("_CodeSignature")
        if "SC_Info" in dirs:
            shutil.rmtree(os.path.join(dp, "SC_Info"), ignore_errors=True)
            dirs.remove("SC_Info")
    log("removed _CodeSignature dirs:", removed)

    items = collect(app)
    items.sort(key=lambda p: p.count(os.sep), reverse=True)
    log("objects to sign:", len(items))

    for p in items:
        base = os.path.basename(p)
        args = ["codesign", "--force", "--sign", IDENT, "--timestamp=none",
                "--generate-entitlement-der"]
        if base.endswith(".appex"):
            bid = plistlib.load(open(os.path.join(p, "Info.plist"), "rb"))["CFBundleIdentifier"]
            if bid in ent_files:
                args += ["--entitlements", ent_files[bid]]
        run(args + [p])

    # 5) 签主 app
    args = ["codesign", "--force", "--sign", IDENT, "--timestamp=none",
            "--generate-entitlement-der", "--entitlements", ent_files[main_bid], app]
    run(args)

    # 6) 校验
    run(["codesign", "--verify", "--deep", "--strict", "--verbose=2", app], check=False)

    # 7) 打包
    out = os.path.abspath("out.ipa")
    if os.path.exists(out):
        os.remove(out)
    run(["ditto", "-c", "-k", "--sequesterRsrc", "--keepParent", "Payload", out], check=True)
    log("ipa:", out, os.path.getsize(out))
    print("RESIGN_OK", out, os.path.getsize(out))
    return


if __name__ == "__main__":
    main()
