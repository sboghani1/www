from receptionist.host_recovery import (
    filesystem_error_lines,
    mount_is_read_only,
)


def test_mount_mode_uses_leading_ro_or_rw_option() -> None:
    assert mount_is_read_only(
        "ro,relatime,discard,errors=remount-ro,commit=30"
    )
    assert not mount_is_read_only(
        "rw,relatime,discard,errors=remount-ro,commit=30"
    )


def test_configured_remount_policy_is_not_an_error() -> None:
    logs = (
        "/dev/vda1 / ext4 rw,relatime,discard,errors=remount-ro,commit=30\n"
        "EXT4-fs (vda1): mounted filesystem with ordered data mode"
    )

    assert filesystem_error_lines(logs) == []


def test_real_ext4_and_io_errors_are_detected() -> None:
    logs = "\n".join(
        [
            "EXT4-fs error (device vda1): ext4_find_entry: inode corrupted",
            "Buffer I/O error on dev vda1, logical block 42",
        ]
    )

    assert filesystem_error_lines(logs) == logs.splitlines()
