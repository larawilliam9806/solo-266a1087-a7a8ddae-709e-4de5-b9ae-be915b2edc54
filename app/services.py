"""业务逻辑：参考/样本/空白对照上传、重验、替换、汇总查询。

空白对照按「实验批次 + 参考版本」生效，只影响同批样本的质控判定：
  * 对照文件只含标准表头 => 有效零检出；含数据行时与样本一样做
    REF 核对、左对齐归一化与重复校验；
  * 每批只使用本版参考下最后一次上传成功的对照；页面所示参考被换下
    或文件校验失败时拒绝提交，已生效对照保持不变；
  * 首页按规范变异拆分统计：同批对照未检出 / 同批对照也检出 /
    缺批次或缺本版对照；判定全部基于当前库内状态实时计算，
    对照替换、样本换批次、参考更新后自动按新状态判定。
"""

from __future__ import annotations

import sqlite3

from .db import read_tx, write_tx
from .normalization import (
    MAX_NAME_LEN,
    ValidationError,
    parse_control_tsv,
    parse_reference,
    parse_tsv,
    validate_and_normalize,
)


# --------------------------------------------------------------------------- #
# 写操作
# --------------------------------------------------------------------------- #
def upload_reference(raw: bytes) -> dict:
    """上传新参考；若已存在样本，全部按新参考重新校验。

    成功后：
      * 通过重验的样本 -> VALID，变体替换为新参考下的归一化结果；
      * REF 不符 / 归一化后重复的样本 -> NEEDS_REUPLOAD，变体删除，记录原因，
        从汇总剔除；
      * 旧参考及旧变体在同一事务中整体替换，失败则全部回滚。
    """
    sequence = parse_reference(raw)
    with write_tx() as conn:
        cur = conn.execute("SELECT id FROM samples")
        old_samples = cur.fetchall()

        cur = conn.execute(
            "INSERT INTO reference_sequences (sequence, length) VALUES (?, ?)",
            (sequence, len(sequence)),
        )
        new_ref_id = cur.lastrowid

        revalidated, failed = [], []
        for row in old_samples:
            outcome = _revalidate_sample(conn, row["id"], new_ref_id, sequence)
            (revalidated if outcome else failed).append(
                conn.execute(
                    "SELECT name, status, error FROM samples WHERE id=?",
                    (row["id"],),
                ).fetchone()
            )
    return {
        "reference_id": new_ref_id,
        "length": len(sequence),
        "revalidated": revalidated,
        "failed": failed,
    }


def upload_sample(name: str, raw: bytes, batch: str | None = None) -> dict:
    """上传（或同名重传）一份样本 TSV，可附带实验批次。

    重传时先完整校验新文件，成功才替换旧版本；旧变体在同一事务中删除、
    新变体写入，任何错误整体回滚，当前结果不受影响。
    同名重传时批次以本次填写为准：未填（或全空白）即为未分批，
    不会继承旧样本的批次。
    """
    name = (name or "").strip()
    if not name:
        raise ValidationError("样本名称不能为空。")
    if len(name) > MAX_NAME_LEN:
        raise ValidationError(f"样本名称不能超过 {MAX_NAME_LEN} 个字符。")
    batch = _clean_batch(batch)

    # 解析在事务外完成（不触碰数据库，不影响现有结果）
    rows = parse_tsv(raw)
    raw_text = raw.decode("utf-8")

    with write_tx() as conn:
        ref_row = conn.execute(
            "SELECT id, sequence FROM reference_sequences ORDER BY id DESC LIMIT 1"
        ).fetchone()
        if ref_row is None:
            raise ValidationError("请先上传参考序列，再上传样本。")
        ref_id, ref_seq = ref_row["id"], ref_row["sequence"]

        # 校验 + 左对齐 + 去冗余；失败抛 ValidationError，事务回滚
        variants = validate_and_normalize(rows, ref_seq)

        old = conn.execute("SELECT id FROM samples WHERE name=?", (name,)).fetchone()
        if old is not None:
            conn.execute("DELETE FROM sample_variants WHERE sample_id=?", (old["id"],))
            conn.execute("DELETE FROM samples WHERE id=?", (old["id"],))

        cur = conn.execute(
            "INSERT INTO samples (name, batch, reference_id, status, error, raw_tsv) "
            "VALUES (?, ?, ?, 'VALID', NULL, ?)",
            (name, batch, ref_id, raw_text),
        )
        sample_id = cur.lastrowid
        _insert_variants(conn, sample_id, variants)

    return {
        "sample": name,
        "batch": batch,
        "count": len(variants),
        "replaced": old is not None,
    }


def upload_control(batch: str, raw: bytes, expected_ref_id: int) -> dict:
    """为某个实验批次提交当前参考下的空白对照 TSV。

    * 文件只含标准表头 => 有效零检出（不写入任何对照变异）；
    * 含数据行时沿用样本的 REF 核对、归一化与重复校验；
    * 每批每版参考只保留最后一次上传成功的对照（同事务内替换）；
    * 页面所示参考已被换下（expected_ref_id 与当前参考不一致）或文件
      校验失败时整体回滚，已生效对照保持不变。
    """
    batch = (batch or "").strip()
    if not batch:
        raise ValidationError("请填写实验批次。")
    if len(batch) > MAX_NAME_LEN:
        raise ValidationError(f"批次名称不能超过 {MAX_NAME_LEN} 个字符。")

    # 解析在事务外完成；仅含标准表头的零检出文件返回空行列表
    rows = parse_control_tsv(raw)
    raw_text = raw.decode("utf-8")

    with write_tx() as conn:
        ref_row = conn.execute(
            "SELECT id, sequence FROM reference_sequences ORDER BY id DESC LIMIT 1"
        ).fetchone()
        if ref_row is None:
            raise ValidationError("请先上传参考序列，再提交空白对照。")
        ref_id, ref_seq = ref_row["id"], ref_row["sequence"]
        if ref_id != expected_ref_id:
            raise ValidationError(
                f"页面所示参考 (#{expected_ref_id}) 已被换下"
                f"（当前版本 #{ref_id}），请刷新页面后重新提交。"
            )

        # 与样本一致的校验 + 左对齐 + 去冗余；失败抛 ValidationError，事务回滚
        variants = validate_and_normalize(rows, ref_seq) if rows else []

        old = conn.execute(
            "SELECT id FROM controls WHERE batch=? AND reference_id=?",
            (batch, ref_id),
        ).fetchone()
        if old is not None:
            conn.execute(
                "DELETE FROM control_variants WHERE control_id=?", (old["id"],)
            )
            conn.execute("DELETE FROM controls WHERE id=?", (old["id"],))

        cur = conn.execute(
            "INSERT INTO controls (batch, reference_id, raw_tsv) VALUES (?, ?, ?)",
            (batch, ref_id, raw_text),
        )
        _insert_control_variants(conn, cur.lastrowid, variants)

    return {"batch": batch, "count": len(variants), "replaced": old is not None}


def delete_sample(sample_id: int) -> None:
    with write_tx() as conn:
        conn.execute("DELETE FROM samples WHERE id=?", (sample_id,))


def _revalidate_sample(
    conn: sqlite3.Connection, sample_id: int, ref_id: int, ref_seq: str
) -> bool:
    """按给定参考重新校验一个样本，返回是否通过。"""
    row = conn.execute(
        "SELECT raw_tsv FROM samples WHERE id=?", (sample_id,)
    ).fetchone()
    try:
        rows = parse_tsv(row["raw_tsv"])
        variants = validate_and_normalize(rows, ref_seq)
    except ValidationError as exc:
        conn.execute("DELETE FROM sample_variants WHERE sample_id=?", (sample_id,))
        conn.execute(
            "UPDATE samples SET reference_id=?, status='NEEDS_REUPLOAD', "
            "error=?, revalidated_at=datetime('now') WHERE id=?",
            (ref_id, str(exc), sample_id),
        )
        return False

    conn.execute("DELETE FROM sample_variants WHERE sample_id=?", (sample_id,))
    conn.execute(
        "UPDATE samples SET reference_id=?, status='VALID', error=NULL, "
        "revalidated_at=datetime('now') WHERE id=?",
        (ref_id, sample_id),
    )
    _insert_variants(conn, sample_id, variants)
    return True


def _insert_variants(
    conn: sqlite3.Connection, sample_id: int, variants: list[dict]
) -> None:
    conn.executemany(
        "INSERT INTO sample_variants "
        "(sample_id, pos, ref, alt, gt, kind, orig_pos, orig_ref, orig_alt) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [
            (
                sample_id,
                v["pos"],
                v["ref"],
                v["alt"],
                v["gt"],
                v["kind"],
                v["orig_pos"],
                v["orig_ref"],
                v["orig_alt"],
            )
            for v in variants
        ],
    )


def _insert_control_variants(
    conn: sqlite3.Connection, control_id: int, variants: list[dict]
) -> None:
    conn.executemany(
        "INSERT INTO control_variants "
        "(control_id, pos, ref, alt, gt, kind, orig_pos, orig_ref, orig_alt) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [
            (
                control_id,
                v["pos"],
                v["ref"],
                v["alt"],
                v["gt"],
                v["kind"],
                v["orig_pos"],
                v["orig_ref"],
                v["orig_alt"],
            )
            for v in variants
        ],
    )


def _clean_batch(batch: str | None) -> str | None:
    """批次名规整：去首尾空白，空串视为未分批（NULL）。"""
    batch = (batch or "").strip()
    if not batch:
        return None
    if len(batch) > MAX_NAME_LEN:
        raise ValidationError(f"批次名称不能超过 {MAX_NAME_LEN} 个字符。")
    return batch


# --------------------------------------------------------------------------- #
# 读操作（同一读事务/快照内完成，页面只展示同一版参考的完整汇总）
# --------------------------------------------------------------------------- #
def get_dashboard() -> dict:
    with read_tx() as conn:
        ref = conn.execute(
            "SELECT * FROM reference_sequences ORDER BY id DESC LIMIT 1"
        ).fetchone()
        if ref is None:
            return {
                "reference": None,
                "stats": {},
                "samples": [],
                "variants": [],
                "controls": [],
            }

        ref_id = ref["id"]
        stats = {
            "valid": conn.execute(
                "SELECT COUNT(*) c FROM samples WHERE reference_id=? AND status='VALID'",
                (ref_id,),
            ).fetchone()["c"],
            "needs_reupload": conn.execute(
                "SELECT COUNT(*) c FROM samples WHERE reference_id=? "
                "AND status='NEEDS_REUPLOAD'",
                (ref_id,),
            ).fetchone()["c"],
            "stale": conn.execute(
                "SELECT COUNT(*) c FROM samples WHERE reference_id IS NOT NULL "
                "AND reference_id<>?",
                (ref_id,),
            ).fetchone()["c"],
            "controls": conn.execute(
                "SELECT COUNT(*) c FROM controls WHERE reference_id=?",
                (ref_id,),
            ).fetchone()["c"],
        }
        samples = conn.execute(
            "SELECT s.*, (SELECT COUNT(*) FROM sample_variants v WHERE v.sample_id=s.id) "
            "AS variant_count FROM samples s ORDER BY s.name"
        ).fetchall()
        # 每个规范变异按同批对照拆成三桶：
        #   c.id 为 NULL            -> 样本缺批次或缺本版对照
        #   对照存在但未含该变异     -> 同批对照未检出
        #   对照存在且含该变异       -> 同批对照也检出
        # controls 有 (batch, reference_id) 唯一约束、control_variants 经重复校验，
        # 每条样本变异最多联到一行对照，三桶之和恒等于检出总数。
        variants = conn.execute(
            "SELECT v.pos, v.ref, v.alt, v.kind, "
            "COUNT(DISTINCT v.sample_id) AS n_samples, "
            "COUNT(DISTINCT CASE WHEN c.id IS NOT NULL AND cv.id IS NULL "
            "      THEN s.id END) AS n_control_clean, "
            "COUNT(DISTINCT CASE WHEN cv.id IS NOT NULL "
            "      THEN s.id END) AS n_control_hit, "
            "COUNT(DISTINCT CASE WHEN c.id IS NULL "
            "      THEN s.id END) AS n_no_control "
            "FROM sample_variants v "
            "JOIN samples s ON s.id = v.sample_id AND s.reference_id=? AND s.status='VALID' "
            "LEFT JOIN controls c ON c.batch = s.batch AND c.reference_id = s.reference_id "
            "LEFT JOIN control_variants cv ON cv.control_id = c.id "
            "  AND cv.pos = v.pos AND cv.ref = v.ref AND cv.alt = v.alt "
            "GROUP BY v.pos, v.ref, v.alt "
            "ORDER BY v.pos, v.ref, v.alt",
            (ref_id,),
        ).fetchall()
        controls = conn.execute(
            "SELECT c.id, c.batch, c.uploaded_at, "
            "(SELECT COUNT(*) FROM control_variants cv WHERE cv.control_id = c.id) "
            "AS variant_count "
            "FROM controls c WHERE c.reference_id=? ORDER BY c.batch",
            (ref_id,),
        ).fetchall()
        return {
            "reference": ref,
            "stats": stats,
            "samples": samples,
            "variants": variants,
            "controls": controls,
        }


def get_variant_detail(pos: int, ref: str, alt: str) -> dict | None:
    with read_tx() as conn:
        current = conn.execute(
            "SELECT id FROM reference_sequences ORDER BY id DESC LIMIT 1"
        ).fetchone()
        if current is None:
            return None
        rows = conn.execute(
            "SELECT s.name AS sample, s.batch AS batch, v.gt, "
            "v.orig_pos, v.orig_ref, v.orig_alt, v.kind, "
            "c.id AS control_id, "
            "cv.orig_pos AS ctl_pos, cv.orig_ref AS ctl_ref, cv.orig_alt AS ctl_alt "
            "FROM sample_variants v "
            "JOIN samples s ON s.id = v.sample_id "
            "LEFT JOIN controls c ON c.batch = s.batch AND c.reference_id = s.reference_id "
            "LEFT JOIN control_variants cv ON cv.control_id = c.id "
            "  AND cv.pos = v.pos AND cv.ref = v.ref AND cv.alt = v.alt "
            "WHERE v.pos=? AND v.ref=? AND v.alt=? "
            "AND s.reference_id=? AND s.status='VALID' "
            "ORDER BY s.name",
            (pos, ref, alt, current["id"]),
        ).fetchall()
        if not rows:
            return None
        annotated = []
        for r in rows:
            if r["control_id"] is None:
                qc = "NO_CONTROL"      # 缺批次或缺本版对照，无法判定
            elif r["ctl_pos"] is not None:
                qc = "CONTROL_HIT"     # 同批对照也检出，需复核
            else:
                qc = "PASS"            # 同批对照未检出
            annotated.append(dict(r, qc=qc))
        return {"pos": pos, "ref": ref, "alt": alt, "rows": annotated}
