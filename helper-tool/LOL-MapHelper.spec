# -*- mode: python ; coding: utf-8 -*-


a = Analysis(
    ['map_app.py'],
    pathex=[],
    binaries=[],
    datas=[('lol_api.py', '.'), ('continent_scan_cli.py', '.'), ('build_map_artifact.py', '.'), ('styles.py', '.')],
    hiddenimports=[],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    # Pillow can use NumPy when it happens to be installed, but the map
    # renderer does not. Excluding it keeps the one-file helper small and
    # noticeably reduces cold-start extraction time.
    excludes=['numpy'],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name='LOL-MapHelper',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
