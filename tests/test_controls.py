"""空白对照（按批次）与逐样本质控判定的端到端测试。

覆盖：
  * 零检出对照（仅标准表头）与非法零检出文件；
  * 对照数据行沿用 REF 核对 / 归一化 / 重复校验；
  * 页面参考被换下或校验失败时拒绝提交、保留已生效对照；
  * 每批只保留本版参考下最后一次上传成功的对照；
  * 首页三桶统计（对照未检出 / 对照也检出 / 缺批次或缺本版对照）；
  * 详情页逐样本质控判定与命中对照的原始位点；
  * 样本同名重传批次以本次填写为准、参考更新后需重交对照。
"""

import io
import os
import tempfile
import threading
import unittest

from app import create_app, services
from app.normalization import ValidationError

REF_V1 = "GCTTTGCACGT"          # 11 bp，含 TTT 重复区
REF_V2 = "GCTATGCACGT"          # 第 4 位 T->A；第 8 位两代均为 A
HEADER = "POS\tREF\tALT\tGT\n"


def tsv(text: str) -> bytes:
    return text.encode("utf-8")


class ControlTestBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        os.environ["DATABASE_PATH"] = os.path.join(self.tmp.name, "ctl.db")
        self.app = create_app()
        self.app.config.update(TESTING=True, SECRET_KEY="test")
        self.client = self.app.test_client()

    def tearDown(self):
        os.environ.pop("DATABASE_PATH", None)
        self.tmp.cleanup()

    # --- 辅助 ------------------------------------------------------------- #
    def upload_ref(self, seq):
        payload = seq.encode() if isinstance(seq, str) else seq
        return self.client.post(
            "/reference", data={"reference": (io.BytesIO(payload), "ref.txt")},
            content_type="multipart/form-data", follow_redirects=True,
        )

    def upload_sample(self, name, content, batch=None):
        data = {"name": name, "sample": (io.BytesIO(content), f"{name}.tsv")}
        if batch is not None:
            data["batch"] = batch
        return self.client.post(
            "/samples", data=data, content_type="multipart/form-data",
            follow_redirects=True,
        )

    def upload_control(self, batch, content, ref_id="CURRENT"):
        if ref_id == "CURRENT":
            ref_id = self.current_ref_id()
        data = {
            "batch": batch,
            "reference_id": str(ref_id),
            "control": (io.BytesIO(content), "control.tsv"),
        }
        return self.client.post(
            "/controls", data=data, content_type="multipart/form-data",
            follow_redirects=True,
        )

    def current_ref_id(self):
        rows = self.db_query(
            "SELECT id FROM reference_sequences ORDER BY id DESC LIMIT 1"
        )
        return rows[0]["id"] if rows else None

    def db_query(self, sql, args=()):
        with self.app.app_context():
            from app.db import get_db
            return get_db().execute(sql, args).fetchall()

    def dash(self):
        with self.app.app_context():
            return services.get_dashboard()

    def variant_row(self, pos, ref, alt):
        for v in self.dash()["variants"]:
            if (v["pos"], v["ref"], v["alt"]) == (pos, ref, alt):
                return v
        return None


class ControlWithoutReferenceTest(ControlTestBase):
    def test_control_before_reference_rejected(self):
        resp = self.upload_control("B1", tsv(HEADER), ref_id=1)
        self.assertIn("请先上传参考序列", resp.get_data(as_text=True))
        self.assertEqual(
            self.db_query("SELECT COUNT(*) c FROM controls")[0]["c"], 0
        )


class BlankControlWorkflowTest(ControlTestBase):
    def setUp(self):
        super().setUp()
        self.upload_ref(REF_V1)

    def test_zero_detection_control_accepted(self):
        resp = self.upload_control("B1", tsv(HEADER))
        self.assertIn("零检出", resp.get_data(as_text=True))
        rows = self.db_query("SELECT * FROM controls")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["batch"], "B1")
        self.assertEqual(
            self.db_query("SELECT COUNT(*) c FROM control_variants")[0]["c"], 0
        )
        # 零检出对照下，同批样本的变异计入「对照未检出」
        self.upload_sample("S1", tsv("8\tA\tC\t0/1\n"), batch="B1")
        v = self.variant_row(8, "A", "C")
        self.assertEqual(v["n_samples"], 1)
        self.assertEqual(v["n_control_clean"], 1)
        self.assertEqual(v["n_control_hit"], 0)
        self.assertEqual(v["n_no_control"], 0)

    def test_empty_file_is_not_valid_zero_detection(self):
        resp = self.upload_control("B1", b"")
        self.assertIn("标准表头", resp.get_data(as_text=True))
        self.assertEqual(
            self.db_query("SELECT COUNT(*) c FROM controls")[0]["c"], 0
        )

    def test_nonstandard_header_only_rejected(self):
        resp = self.upload_control("B1", tsv("a\tb\tc\td\n"))
        self.assertIn("标准表头", resp.get_data(as_text=True))
        resp = self.upload_control("B1", tsv("POS\tREF\tALT\n"))
        self.assertIn("标准表头", resp.get_data(as_text=True))
        self.assertEqual(
            self.db_query("SELECT COUNT(*) c FROM controls")[0]["c"], 0
        )

    def test_control_rows_validated_like_samples(self):
        # REF 不符 -> 拒绝
        resp = self.upload_control("B1", tsv("1\tT\tC\t0/1\n"))
        self.assertIn("REF 核对失败", resp.get_data(as_text=True))
        # 归一化后重复 -> 整份拒绝
        resp = self.upload_control("B1", tsv("3\tT\tTT\t0/1\n5\tT\tTT\t0/1\n"))
        self.assertIn("同一变异", resp.get_data(as_text=True))
        self.assertEqual(
            self.db_query("SELECT COUNT(*) c FROM controls")[0]["c"], 0
        )
        # 合法数据行 -> 归一化入库，保留原始位点
        resp = self.upload_control("B1", tsv(HEADER + "3\tT\tTT\t0/1\n"))
        self.assertIn("1 条变异", resp.get_data(as_text=True))
        row = self.db_query("SELECT * FROM control_variants")[0]
        self.assertEqual((row["pos"], row["ref"], row["alt"]), (2, "C", "CT"))
        self.assertEqual((row["orig_pos"], row["orig_ref"], row["orig_alt"]),
                         (3, "T", "TT"))

    def test_control_requires_batch_and_page_reference(self):
        resp = self.upload_control("", tsv(HEADER))
        self.assertIn("请填写实验批次", resp.get_data(as_text=True))
        # 缺少页面参考版本字段
        resp = self.client.post(
            "/controls",
            data={"batch": "B1", "control": (io.BytesIO(tsv(HEADER)), "c.tsv")},
            content_type="multipart/form-data", follow_redirects=True,
        )
        self.assertIn("参考版本", resp.get_data(as_text=True))
        self.assertEqual(
            self.db_query("SELECT COUNT(*) c FROM controls")[0]["c"], 0
        )

    def test_control_hit_only_affects_same_batch(self):
        # B1 对照检出插入 (2,C,CT)；B2 对照零检出；B3 无对照；S4 无批次
        self.upload_control("B1", tsv("3\tT\tTT\t0/1\n"))
        self.upload_control("B2", tsv(HEADER))
        self.upload_sample("S1", tsv("5\tT\tTT\t0/1\n"), batch="B1")  # 等价书写
        self.upload_sample("S2", tsv("4\tT\tTT\t0/1\n"), batch="B2")
        self.upload_sample("S3", tsv("3\tT\tTT\t0/1\n"), batch="B3")
        self.upload_sample("S4", tsv("3\tT\tTT\t0/1\n"))               # 未分批
        v = self.variant_row(2, "C", "CT")
        self.assertEqual(v["n_samples"], 4)
        self.assertEqual(v["n_control_hit"], 1)    # 仅 S1：同批对照也检出
        self.assertEqual(v["n_control_clean"], 1)  # 仅 S2：同批对照未检出
        self.assertEqual(v["n_no_control"], 2)     # S3 缺本版对照 + S4 缺批次

    def test_last_successful_control_wins_and_failure_preserves(self):
        self.upload_control("B1", tsv("8\tA\tC\t0/1\n"))
        self.upload_sample("S1", tsv("8\tA\tC\t0/1\n"), batch="B1")
        self.assertEqual(self.variant_row(8, "A", "C")["n_control_hit"], 1)
        # 零检出对照替换旧对照 -> 立即按新状态判定为「对照未检出」
        self.upload_control("B1", tsv(HEADER))
        v = self.variant_row(8, "A", "C")
        self.assertEqual(v["n_control_hit"], 0)
        self.assertEqual(v["n_control_clean"], 1)
        self.assertEqual(
            self.db_query("SELECT COUNT(*) c FROM controls")[0]["c"], 1
        )
        # 校验失败 -> 拒绝本次提交，已生效对照保留
        resp = self.upload_control("B1", tsv("1\tT\tC\t0/1\n"))
        self.assertIn("已生效对照不变", resp.get_data(as_text=True))
        v = self.variant_row(8, "A", "C")
        self.assertEqual(v["n_control_clean"], 1)
        self.assertEqual(
            self.db_query("SELECT COUNT(*) c FROM controls")[0]["c"], 1
        )

    def test_stale_page_reference_rejected(self):
        self.upload_control("B1", tsv(HEADER))
        old_ref = self.current_ref_id()
        self.upload_ref(REF_V2)
        # 页面所示参考已被换下 -> 拒绝提交，旧对照记录保留但不再生效
        resp = self.upload_control("B1", tsv("8\tA\tC\t0/1\n"), ref_id=old_ref)
        self.assertIn("已被换下", resp.get_data(as_text=True))
        self.assertEqual(
            self.db_query("SELECT COUNT(*) c FROM controls")[0]["c"], 1
        )
        self.assertEqual(self.dash()["controls"], [])

    def test_reference_update_requires_new_controls(self):
        self.upload_control("B1", tsv("8\tA\tC\t0/1\n"))
        self.upload_sample("S1", tsv("8\tA\tC\t0/1\n"), batch="B1")
        self.assertEqual(self.variant_row(8, "A", "C")["n_control_hit"], 1)
        # 换参考：S1 的 (8,A,C) 在 V2 下仍合法，重验通过；对照属于旧版参考
        self.upload_ref(REF_V2)
        v = self.variant_row(8, "A", "C")
        self.assertIsNotNone(v)
        self.assertEqual(v["n_control_hit"], 0)
        self.assertEqual(v["n_no_control"], 1)  # 缺本版对照
        # 新参考下重新提交对照后恢复判定
        self.upload_control("B1", tsv("8\tA\tC\t0/1\n"))
        v = self.variant_row(8, "A", "C")
        self.assertEqual(v["n_control_hit"], 1)
        self.assertEqual(v["n_no_control"], 0)

    def test_sample_batch_reupload_semantics(self):
        self.upload_control("B1", tsv(HEADER))
        self.upload_sample("S1", tsv("8\tA\tC\t0/1\n"), batch="B1")
        self.assertEqual(self.variant_row(8, "A", "C")["n_control_clean"], 1)
        # 同名重传未填批次 -> 以本次填写为准，视为未分批（不继承旧批次）
        self.upload_sample("S1", tsv("8\tA\tC\t0/1\n"))
        row = self.db_query("SELECT batch FROM samples WHERE name='S1'")[0]
        self.assertIsNone(row["batch"])
        self.assertEqual(self.variant_row(8, "A", "C")["n_no_control"], 1)
        # 再次重传填写批次 -> 以本次填写为准
        self.upload_sample("S1", tsv("8\tA\tC\t0/1\n"), batch="B1")
        row = self.db_query("SELECT batch FROM samples WHERE name='S1'")[0]
        self.assertEqual(row["batch"], "B1")
        self.assertEqual(self.variant_row(8, "A", "C")["n_control_clean"], 1)

    def test_sample_batch_change_rejudges(self):
        self.upload_control("B1", tsv("8\tA\tC\t0/1\n"))  # B1 对照检出该变异
        self.upload_control("B2", tsv(HEADER))            # B2 零检出
        self.upload_sample("S1", tsv("8\tA\tC\t0/1\n"), batch="B1")
        self.assertEqual(self.variant_row(8, "A", "C")["n_control_hit"], 1)
        # 样本换批次 -> 按新批次的对照重新判定
        self.upload_sample("S1", tsv("8\tA\tC\t0/1\n"), batch="B2")
        v = self.variant_row(8, "A", "C")
        self.assertEqual(v["n_control_hit"], 0)
        self.assertEqual(v["n_control_clean"], 1)

    def test_detail_page_qc_and_control_locus(self):
        self.upload_control("B1", tsv("3\tT\tTT\t0/1\n"))  # 对照原始位点 pos=3
        self.upload_control("B2", tsv(HEADER))
        self.upload_sample("S1", tsv("5\tT\tTT\t0/1\n"), batch="B1")
        self.upload_sample("S2", tsv("4\tT\tTT\t1/1\n"), batch="B2")
        self.upload_sample("S3", tsv("3\tT\tTT\t0/1\n"))

        with self.app.app_context():
            detail = services.get_variant_detail(2, "C", "CT")
        rows = {r["sample"]: r for r in detail["rows"]}
        self.assertEqual(rows["S1"]["qc"], "CONTROL_HIT")
        # 命中对照的原始位点与等位基因
        self.assertEqual(rows["S1"]["ctl_pos"], 3)
        self.assertEqual(rows["S1"]["ctl_ref"], "T")
        self.assertEqual(rows["S1"]["ctl_alt"], "TT")
        self.assertEqual(rows["S2"]["qc"], "PASS")
        self.assertIsNone(rows["S2"]["ctl_pos"])
        self.assertEqual(rows["S3"]["qc"], "NO_CONTROL")

        page = self.client.get("/variants/2/C>CT").get_data(as_text=True)
        self.assertIn("对照也检出", page)
        self.assertIn("通过", page)
        self.assertIn("缺对照", page)
        self.assertIn("B1", page)

    def test_dashboard_page_shows_batches_controls_and_buckets(self):
        self.upload_control("B1", tsv(HEADER))
        self.upload_sample("S1", tsv("8\tA\tC\t0/1\n"), batch="B1")
        page = self.client.get("/").get_data(as_text=True)
        self.assertIn("B1", page)
        self.assertIn("对照未检出", page)
        self.assertIn("对照也检出", page)
        self.assertIn("缺批次或缺本版对照", page)
        self.assertIn("空白对照", page)


class ControlConcurrencyTest(unittest.TestCase):
    """对照/样本/参考交错提交时，每页快照内的三桶统计始终自洽。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        os.environ["DATABASE_PATH"] = os.path.join(self.tmp.name, "conc.db")
        self.app = create_app()
        self.app.config.update(TESTING=True, SECRET_KEY="test")
        self.errors: list[Exception] = []

    def tearDown(self):
        os.environ.pop("DATABASE_PATH", None)
        self.tmp.cleanup()

    def test_mixed_workload_stays_consistent(self):
        with self.app.app_context():
            services.upload_reference(REF_V1)

        def upload_samples():
            for i in range(12):
                try:
                    with self.app.app_context():
                        services.upload_sample(
                            f"S{i % 4}",
                            tsv("4\tT\tTT\t0/1\n"),
                            batch=None if i % 4 == 0 else f"B{i % 3}",
                        )
                except ValidationError:
                    pass  # 参考轮换导致 REF 不符属预期竞争结果
                except Exception as exc:
                    self.errors.append(exc)

        def upload_controls():
            for i in range(10):
                try:
                    with self.app.app_context():
                        ref_id = services.get_dashboard()["reference"]["id"]
                        content = tsv(HEADER) if i % 2 else tsv("4\tT\tTT\t0/1\n")
                        services.upload_control(f"B{i % 3}", content, ref_id)
                except ValidationError:
                    pass  # 页面参考被换下 / 校验失败属预期拒绝
                except Exception as exc:
                    self.errors.append(exc)

        def rotate_references():
            for ref in (REF_V2, REF_V1):
                try:
                    with self.app.app_context():
                        services.upload_reference(ref)
                except Exception as exc:
                    self.errors.append(exc)

        def reader():
            for _ in range(30):
                try:
                    with self.app.app_context():
                        dash = services.get_dashboard()
                        for v in dash["variants"]:
                            # 不变量：三桶之和恒等于检出总数
                            self.assertEqual(
                                v["n_samples"],
                                v["n_control_clean"]
                                + v["n_control_hit"]
                                + v["n_no_control"],
                            )
                        # 不变量：页面只展示本版参考下生效的对照
                        for c in dash["controls"]:
                            self.assertTrue(c["batch"])
                        detail = services.get_variant_detail(2, "C", "CT")
                        if detail is not None:
                            for r in detail["rows"]:
                                self.assertIn(
                                    r["qc"],
                                    ("PASS", "CONTROL_HIT", "NO_CONTROL"),
                                )
                except Exception as exc:
                    self.errors.append(exc)

        threads = [
            threading.Thread(target=f)
            for f in (upload_samples, upload_controls, rotate_references,
                      reader, reader)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)
            self.assertFalse(t.is_alive(), "线程未在超时内结束，可能存在锁死锁")

        if self.errors:
            raise AssertionError(
                "并发过程出现非预期异常:\n"
                + "\n".join(f"{type(e).__name__}: {e}" for e in self.errors)
            )


if __name__ == "__main__":
    unittest.main()
