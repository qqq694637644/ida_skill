from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from skill_temple.app import create_app
from skill_temple.evals import evaluate_file
from skill_temple.runtime import (
    DEFAULT_MANIFEST_MAX_CHARS,
    DEFAULT_MAX_SKILLS,
    RETRIEVE_INSTRUCTIONS_MAX_CHARS,
    SKILL_CATALOG_MAX_CHARS,
    SKILL_DESCRIPTION_MAX_CHARS,
    SKILL_NAME_MAX_CHARS,
    SkillPathError,
    SkillRuntime,
    SkillRuntimeError,
    load_runtime,
)


def _write_skill(
    skills_root: Path,
    skill_id: str,
    description: str,
    body: str,
    docs: dict[str, str] | None = None,
) -> Path:
    skill_root = skills_root / skill_id
    skill_root.mkdir(parents=True, exist_ok=True)
    (skill_root / "SKILL.md").write_text(
        "\n".join(
            [
                "---",
                f"name: {skill_id}",
                f"description: {description}",
                "---",
                "",
                body,
                "",
            ]
        ),
        encoding="utf-8",
    )
    for relative_path, content in (docs or {}).items():
        path = skill_root / relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    return skill_root


class RuntimeTests(unittest.TestCase):
    def test_packaged_example_runtime_lists_discovery_metadata_only(self) -> None:
        runtime = load_runtime()
        result = runtime.list_skills()

        skill = next(item for item in result["skills"] if item["skill_id"] == "idapython")
        self.assertEqual(skill["entrypoint"], "SKILL.md")
        self.assertIn("IDAPython", skill["description"])
        self.assertTrue(skill["content_hash"].startswith("sha256:"))
        self.assertNotIn("instructions", skill)
        self.assertNotIn("docs", skill)

    def test_resolve_uses_exact_mentions_and_explicit_hints(self) -> None:
        runtime = load_runtime()
        at_mention = runtime.resolve("@idapython write a Hex-Rays ctree visitor")
        dollar_mention = runtime.resolve("use $idapython for this task")
        hinted = runtime.resolve("反编译 main 函数", hinted_skill_ids=["idapython"])

        for result in [at_mention, dollar_mention, hinted]:
            self.assertEqual(result["matches"][0]["skill_id"], "idapython")
            self.assertIn("description", result["matches"][0])
            self.assertNotIn("confidence", result["matches"][0])
            self.assertEqual(result["available_skills"][0]["skill_id"], "idapython")

    def test_resolve_does_not_match_skill_name_inside_another_word(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            skills_root = Path(temp_dir) / "skills"
            _write_skill(skills_root, "ida", "Use for IDA analysis.", "# IDA")
            runtime = SkillRuntime(skills_root)

            self.assertEqual(runtime.resolve("validate this configuration")["matches"], [])
            explicit = runtime.resolve("use @ida for this database")
            self.assertEqual(explicit["matches"][0]["skill_id"], "ida")

    def test_resolve_does_not_make_server_side_semantic_selection(self) -> None:
        runtime = load_runtime()

        for query in [
            "please use this tool",
            "use this configuration",
            "analysis request",
            "请使用这个工具",
            "请帮我处理这个请求",
            "分析这个配置",
            "find xrefs in IDA",
            "Hex-Rays decompile",
            "反编译 main 函数",
            "查找 strcpy 的交叉引用",
            "用 IDAPython 批量重命名函数",
            "给地址打补丁",
            "函数求导怎么做",
            "解释 Python 函数类型注解",
            "把这个文件重命名",
            "给软件安装安全补丁",
            "我的家庭地址是什么",
            "解释大端和小端字节序",
            "编译这个 Rust 项目",
            "做一次交叉验证",
            "分析数据库类型设计",
        ]:
            with self.subTest(query=query):
                result = runtime.resolve(query)
                self.assertEqual(result["matches"], [])
                self.assertEqual(result["available_skills"][0]["skill_id"], "idapython")

    def test_non_codex_frontmatter_fields_do_not_trigger_selection(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            skills_root = Path(temp_dir) / "skills"
            skill_root = skills_root / "demo"
            skill_root.mkdir(parents=True)
            (skill_root / "SKILL.md").write_text(
                "---\n"
                "name: demo\n"
                "description: Use for demonstration tasks.\n"
                "aliases: 演示工具\n"
                "keywords:\n"
                "  - 中文发现\n"
                "  - multilingual\n"
                "---\n\n# Demo\n",
                encoding="utf-8",
            )

            runtime = SkillRuntime(skills_root)

            self.assertEqual(runtime.resolve("请执行中文发现")["matches"], [])
            self.assertEqual(runtime.resolve("使用演示工具")["matches"], [])
            selected = runtime.resolve("中文任务", hinted_skill_ids=["demo"])
            self.assertEqual(selected["matches"][0]["skill_id"], "demo")

    def test_retrieve_returns_complete_skill_entrypoint(self) -> None:
        runtime = load_runtime()
        result = runtime.retrieve(
            "@idapython write a script to find xrefs to strcpy",
            hinted_skill_ids=["idapython"],
        )

        selected = result["selected_skills"][0]
        self.assertEqual(selected["skill_id"], "idapython")
        self.assertEqual(selected["role"], "primary")
        self.assertEqual(selected["source_path"], "SKILL.md")
        self.assertIn("name: idapython", selected["instructions"])
        self.assertIn("## Progressive disclosure", selected["instructions"])
        self.assertFalse(selected["truncated"])
        self.assertIsNone(selected["next_start_line"])
        self.assertIn("docs/idautils.md", selected["referenced_paths"])
        self.assertIn("docs/ida_hexrays.md", selected["referenced_paths"])
        self.assertNotIn("confidence", selected)
        self.assertNotIn("operating_rules", selected)
        self.assertNotIn("response_contract", selected)
        self.assertNotIn("evidence", selected)
        self.assertNotIn("debug", result)
        self.assertTrue(result["decision"]["selected"])
        self.assertTrue(result["decision"]["stop_retrieval"])
        self.assertEqual(result["decision"]["next_action"], "followSkillInstructions")

    def test_retrieve_debug_keeps_diagnostics_separate(self) -> None:
        runtime = load_runtime()
        result = runtime.retrieve(
            "@idapython write a script to find xrefs to strcpy",
            hinted_skill_ids=["idapython"],
            include_debug=True,
        )

        selected = result["selected_skills"][0]
        self.assertIn("why_selected", selected["debug"])
        self.assertGreaterEqual(result["debug"]["available_skill_count"], 1)
        self.assertFalse(result["debug"]["allow_skill_chaining_requested"])
        self.assertFalse(result["debug"]["automatic_skill_chaining"])
        self.assertTrue(result["debug"]["resolved_matches"])

    def test_search_finds_reference_then_recommends_read(self) -> None:
        runtime = load_runtime()
        result = runtime.search("idapython", "ctree_visitor_t cot_call", limit=3)

        self.assertTrue(result["matches"])
        self.assertEqual(result["mode"], "keyword")
        self.assertEqual(result["engine"], "sqlite_fts5_symbol_index")
        self.assertEqual(result["matches"][0]["path"], "docs/ida_hexrays.md")
        self.assertIn("ctree", result["matches"][0]["excerpt"].lower())
        self.assertIn("ctree_visitor_t", result["matches"][0]["symbols"])
        self.assertEqual(result["recommended_next_action"], "readSkillContent")

    def test_search_rejects_non_keyword_mode(self) -> None:
        runtime = load_runtime()
        with self.assertRaisesRegex(RuntimeError, "Only keyword search mode"):
            runtime.search("idapython", "ctree visitor", mode="hybrid")

    def test_read_returns_continuation_for_partial_file(self) -> None:
        runtime = load_runtime()
        result = runtime.read("idapython", "SKILL.md", start_line=1, max_lines=5)

        self.assertEqual(result["path"], "SKILL.md")
        self.assertEqual(result["start_line"], 1)
        self.assertEqual(result["end_line"], 5)
        self.assertIn("name: idapython", result["content"])
        self.assertTrue(result["truncated"])
        self.assertEqual(result["next_start_line"], 6)

    def test_read_returns_an_oversized_single_line_without_losing_content(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            skills_root = Path(temp_dir) / "skills"
            long_line = "X" * 100
            _write_skill(
                skills_root,
                "demo",
                "Use for long-line tests.",
                "# Demo",
                {"docs/long.txt": long_line},
            )
            runtime = SkillRuntime(skills_root)

            result = runtime.read("demo", "docs/long.txt", max_chars=10)

            self.assertEqual(result["content"], long_line)
            self.assertFalse(result["truncated"])
            self.assertIsNone(result["next_start_line"])

    def test_read_rejects_unsafe_or_invalid_ranges(self) -> None:
        runtime = load_runtime()
        for path in ["../pyproject.toml", "/etc/passwd", "docs/../../SKILL.md"]:
            with self.subTest(path=path):
                with self.assertRaises(SkillPathError):
                    runtime.read("idapython", path)
        with self.assertRaises(SkillPathError):
            runtime.read("idapython", "SKILL.md", start_line=99999)

    def test_runtime_can_load_skills_dir_from_cwd_dotenv(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            tmp_path = Path(temp_dir)
            skills_root = tmp_path / "custom_skills"
            _write_skill(
                skills_root,
                "demo",
                "Use for dotenv-demo tasks.",
                "# Demo\n\nRead `docs/demo.md`.",
                {"docs/demo.md": "# Demo docs\n\ndotenv-demo content.\n"},
            )
            (tmp_path / ".env").write_text(
                f'SKILL_TEMPLE_SKILLS_DIR = "{skills_root}"\n',
                encoding="utf-8",
            )

            previous_cwd = Path.cwd()
            try:
                os.chdir(tmp_path)
                with patch.dict(os.environ, {"SKILL_TEMPLE_SKILLS_DIR": ""}, clear=False):
                    runtime = load_runtime()
            finally:
                os.chdir(previous_cwd)

            self.assertEqual(runtime.skills_dir, skills_root.resolve())
            result = runtime.retrieve("@demo dotenv-demo", hinted_skill_ids=["demo"])
            self.assertEqual(result["selected_skills"][0]["skill_id"], "demo")

    def test_runtime_loads_skill_without_json_or_index(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            skills_root = Path(temp_dir) / "skills"
            skill_root = _write_skill(
                skills_root,
                "demo",
                "Use for unittest-demo tasks.",
                "# Demo\n\nRead `docs/demo.md` for details.",
                {"docs/demo.md": "# Demo docs\n\nunittest-demo content.\n"},
            )

            runtime = SkillRuntime(skills_root)
            result = runtime.retrieve("@demo unittest-demo", hinted_skill_ids=["demo"])

            self.assertEqual(result["selected_skills"][0]["referenced_paths"], ["docs/demo.md"])
            self.assertFalse((skill_root / "skill.json").exists())
            self.assertFalse((skill_root / "INDEX.md").exists())

    def test_runtime_discovers_nested_skill_directories(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            skills_root = Path(temp_dir) / "skills"
            _write_skill(
                skills_root / "group",
                "nested",
                "Use for nested-discovery tasks.",
                "# Nested\n\nUse nested instructions.",
            )

            runtime = SkillRuntime(skills_root)
            result = runtime.retrieve("@nested task", hinted_skill_ids=["nested"])
            self.assertEqual(result["selected_skills"][0]["skill_id"], "nested")

    def test_runtime_prunes_directories_beyond_the_scan_depth(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            skills_root = Path(temp_dir) / "skills"
            accepted_parent = skills_root.joinpath(*[f"a{index}" for index in range(5)])
            rejected_parent = skills_root.joinpath(*[f"b{index}" for index in range(6)])
            _write_skill(accepted_parent, "accepted", "Use for accepted tasks.", "# Accepted")
            _write_skill(rejected_parent, "rejected", "Use for rejected tasks.", "# Rejected")

            runtime = SkillRuntime(skills_root)
            skill_ids = {skill["skill_id"] for skill in runtime.list_skills()["skills"]}

            self.assertIn("accepted", skill_ids)
            self.assertNotIn("rejected", skill_ids)

    def test_duplicate_frontmatter_names_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            skills_root = Path(temp_dir) / "skills"
            _write_skill(skills_root / "one", "same", "Use for one.", "# One")
            _write_skill(skills_root / "two", "same", "Use for two.", "# Two")
            with self.assertRaisesRegex(SkillRuntimeError, "Duplicate skill name"):
                SkillRuntime(skills_root)

    def test_missing_frontmatter_description_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            skills_root = Path(temp_dir) / "skills"
            skill_root = skills_root / "broken"
            skill_root.mkdir(parents=True)
            (skill_root / "SKILL.md").write_text(
                "---\nname: broken\n---\n\n# Broken\n",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(SkillRuntimeError, "missing frontmatter description"):
                SkillRuntime(skills_root)

    def test_frontmatter_name_and_description_lengths_are_bounded(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            skills_root = Path(temp_dir) / "skills"
            _write_skill(
                skills_root,
                "n" * (SKILL_NAME_MAX_CHARS + 1),
                "Use for long-name tests.",
                "# Long name",
            )
            with self.assertRaisesRegex(SkillRuntimeError, "name exceeds"):
                SkillRuntime(skills_root)

        with tempfile.TemporaryDirectory() as temp_dir:
            skills_root = Path(temp_dir) / "skills"
            _write_skill(
                skills_root,
                "long-description",
                "D" * (SKILL_DESCRIPTION_MAX_CHARS + 1),
                "# Long description",
            )
            with self.assertRaisesRegex(SkillRuntimeError, "description exceeds"):
                SkillRuntime(skills_root)

    def test_multiple_explicit_skills_auto_chain_without_a_flag(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            skills_root = Path(temp_dir) / "skills"
            for skill_id in ["alpha", "beta"]:
                _write_skill(
                    skills_root,
                    skill_id,
                    f"Use the {skill_id} skill for shared-task work.",
                    f"# {skill_id}\n\nUse {skill_id} instructions.",
                )

            runtime = SkillRuntime(skills_root)
            mentioned = runtime.retrieve(
                "@alpha $beta shared-task",
            )
            hinted = runtime.retrieve(
                "shared-task",
                hinted_skill_ids=["alpha", "beta"],
            )
            chained = runtime.retrieve(
                "@alpha @beta shared-task",
                hinted_skill_ids=["alpha", "beta"],
                max_skills=2,
                allow_skill_chaining=True,
                include_debug=True,
            )

            self.assertEqual(
                [item["skill_id"] for item in mentioned["selected_skills"]],
                ["alpha", "beta"],
            )
            self.assertEqual(len(hinted["selected_skills"]), 2)
            self.assertEqual(len(chained["selected_skills"]), 2)
            self.assertEqual(chained["selected_skills"][0]["role"], "primary")
            self.assertEqual(chained["selected_skills"][1]["role"], "secondary")
            self.assertTrue(chained["debug"]["allow_skill_chaining_requested"])
            self.assertTrue(chained["debug"]["automatic_skill_chaining"])
            self.assertFalse(hinted["catalog_included"])
            self.assertEqual(hinted["available_skills"], [])

    def test_too_many_explicit_skills_are_not_partially_loaded(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            skills_root = Path(temp_dir) / "skills"
            skill_ids = ["alpha", "beta", "gamma", "delta"]
            for skill_id in skill_ids:
                _write_skill(
                    skills_root,
                    skill_id,
                    f"Use {skill_id} for explicit selection tests.",
                    f"# {skill_id}",
                )

            runtime = SkillRuntime(skills_root)
            result = runtime.retrieve("@alpha @beta @gamma @delta do work")
            hinted = runtime.retrieve("do work", hinted_skill_ids=skill_ids)

            for response in [result, hinted]:
                self.assertEqual(response["selected_skills"], [])
                self.assertEqual(response["explicit_skill_ids"], skill_ids)
                self.assertEqual(
                    response["omitted_explicit_skill_ids"],
                    skill_ids[DEFAULT_MAX_SKILLS:],
                )
                self.assertEqual(
                    response["decision"]["next_action"],
                    "retryWithFewerSkills",
                )
                self.assertFalse(response["decision"]["selected"])
                self.assertTrue(response["catalog_included"])

    def test_unknown_explicit_mentions_are_reported(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            skills_root = Path(temp_dir) / "skills"
            _write_skill(skills_root, "alpha", "Use for alpha tasks.", "# Alpha")
            runtime = SkillRuntime(skills_root)

            missing = runtime.retrieve("@missing do work")
            self.assertEqual(missing["selected_skills"], [])
            self.assertEqual(missing["unknown_skill_mentions"], ["missing"])
            self.assertEqual(missing["decision"]["next_action"], "selectSkillOrAnswer")
            self.assertIn("unavailable", missing["decision"]["reason"].lower())

            mixed = runtime.retrieve("$alpha @missing do work")
            self.assertEqual(mixed["selected_skills"][0]["skill_id"], "alpha")
            self.assertEqual(mixed["unknown_skill_mentions"], ["missing"])
            self.assertIn("missing", mixed["decision"]["reason"])
            self.assertFalse(mixed["catalog_included"])

    def test_multiple_skills_share_a_global_instruction_budget(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            skills_root = Path(temp_dir) / "skills"
            large_body = "# Large\n\n" + "\n".join("X" * 200 for _ in range(220))
            skill_ids = ["alpha", "beta", "gamma"]
            for skill_id in skill_ids:
                _write_skill(
                    skills_root,
                    skill_id,
                    f"Use {skill_id} for shared-budget tasks.",
                    large_body,
                )
            runtime = SkillRuntime(skills_root)

            result = runtime.retrieve(
                "@alpha @beta @gamma shared-budget",
                hinted_skill_ids=skill_ids,
                max_skills=3,
                allow_skill_chaining=True,
                include_debug=True,
            )

            instruction_lengths = [
                len(packet["instructions"]) for packet in result["selected_skills"]
            ]
            self.assertLessEqual(sum(instruction_lengths), RETRIEVE_INSTRUCTIONS_MAX_CHARS)
            self.assertTrue(
                all(length <= DEFAULT_MANIFEST_MAX_CHARS for length in instruction_lengths)
            )
            self.assertTrue(all(packet["truncated"] for packet in result["selected_skills"]))
            self.assertEqual(
                result["debug"]["used_instruction_chars"],
                sum(instruction_lengths),
            )
            self.assertEqual(
                result["debug"]["instruction_char_limit"],
                RETRIEVE_INSTRUCTIONS_MAX_CHARS,
            )
            self.assertLess(len(json.dumps(result, ensure_ascii=False)), 100_000)

    def test_retrieve_returns_catalog_before_model_selects_a_skill(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            skills_root = Path(temp_dir) / "skills"
            _write_skill(
                skills_root,
                "alpha",
                "Use exclusively for alpha-only-specialized work.",
                "# Alpha",
            )
            runtime = SkillRuntime(skills_root)
            result = runtime.retrieve("unrelated cooking recipe")

            self.assertEqual(result["selected_skills"], [])
            self.assertFalse(result["decision"]["selected"])
            self.assertEqual(result["decision"]["next_action"], "selectSkillOrAnswer")
            self.assertFalse(result["decision"]["stop_retrieval"])
            self.assertEqual(result["available_skills"][0]["skill_id"], "alpha")

            selected = runtime.retrieve(
                "unrelated cooking recipe",
                hinted_skill_ids=["alpha"],
            )
            self.assertEqual(selected["selected_skills"][0]["skill_id"], "alpha")
            self.assertEqual(selected["available_skills"], [])
            self.assertFalse(selected["catalog_included"])

    def test_skill_catalog_has_an_independent_response_budget(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            skills_root = Path(temp_dir) / "skills"
            for index in range(140):
                skill_id = f"skill{index:03d}"
                _write_skill(
                    skills_root,
                    skill_id,
                    f"Use {skill_id} for specialized work. " + "D" * 850,
                    f"# {skill_id}",
                )

            runtime = SkillRuntime(skills_root)
            result = runtime.retrieve("find a suitable skill")
            catalog_text = json.dumps(
                result["available_skills"],
                ensure_ascii=False,
                separators=(",", ":"),
            )

            self.assertLessEqual(len(catalog_text), SKILL_CATALOG_MAX_CHARS)
            self.assertEqual(result["available_skill_count"], 140)
            self.assertEqual(
                result["included_skill_count"] + result["omitted_skill_count"],
                140,
            )
            self.assertLess(result["included_skill_count"], 140)
            self.assertTrue(result["catalog_included"])
            self.assertLess(len(json.dumps(result, ensure_ascii=False)), 100_000)

            client = TestClient(create_app(skills_root))
            discovery = client.post(
                "/v1/skills/retrieve",
                json={"query": "find a suitable skill"},
            )
            self.assertEqual(discovery.status_code, 200)
            self.assertLess(len(discovery.text), 100_000)

            selected = client.post(
                "/v1/skills/retrieve",
                json={
                    "query": "use the selected skill",
                    "hinted_skill_ids": ["skill000"],
                },
            )
            self.assertEqual(selected.status_code, 200)
            self.assertEqual(selected.json()["available_skills"], [])
            self.assertFalse(selected.json()["catalog_included"])
            self.assertLess(len(selected.text), 100_000)

    def test_skill_entrypoint_hash_is_cached_for_catalog_and_retrieval(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            skills_root = Path(temp_dir) / "skills"
            _write_skill(skills_root, "alpha", "Use for alpha tasks.", "# Alpha")
            runtime = SkillRuntime(skills_root)

            with patch(
                "skill_temple.runtime._content_hash",
                side_effect=AssertionError("entrypoint hash should be cached"),
            ):
                catalog = runtime.resolve("no explicit selection")
                selected = runtime.retrieve("$alpha do work")

            self.assertTrue(catalog["available_skills"][0]["content_hash"])
            self.assertEqual(selected["selected_skills"][0]["skill_id"], "alpha")

    def test_default_openapi_exposes_only_task_operations(self) -> None:
        schema = create_app().openapi()
        operation_ids = {
            operation["operationId"]
            for path_item in schema["paths"].values()
            for operation in path_item.values()
        }
        self.assertEqual(
            operation_ids,
            {
                "retrieveSkillContext",
                "searchSkillDocs",
                "readSkillContent",
                "listIdaInstances",
                "getIdaDatabaseInfo",
                "listIdaFunctions",
                "decompileIdaFunction",
                "getIdaXrefs",
                "executeIdapython",
            },
        )
        for path, path_item in schema["paths"].items():
            for method, operation in path_item.items():
                with self.subTest(path=path, method=method):
                    self.assertLessEqual(len(operation.get("description", "")), 300)
                    self.assertIs(operation.get("x-openai-isConsequential"), False)

        retrieve_schema = schema["components"]["schemas"]["RetrieveSkillContextRequest"]
        self.assertEqual(
            set(retrieve_schema["properties"]),
            {"query", "hinted_skill_ids", "allow_skill_chaining"},
        )
        selected_schema = schema["components"]["schemas"]["SelectedSkillPacket"]
        self.assertNotIn("confidence", selected_schema["properties"])
        for field in [
            "source_path",
            "instructions",
            "referenced_paths",
            "truncated",
            "next_start_line",
        ]:
            self.assertIn(field, selected_schema["properties"])
        retrieve_response = schema["components"]["schemas"]["RetrieveSkillContextResponse"]
        self.assertIn("available_skills", retrieve_response["properties"])
        available_schema = schema["components"]["schemas"]["AvailableSkillMetadata"]
        self.assertEqual(
            set(available_schema["properties"]),
            {
                "skill_id",
                "name",
                "description",
                "description_truncated",
                "entrypoint",
                "content_hash",
            },
        )
        for field in [
            "available_skill_count",
            "included_skill_count",
            "omitted_skill_count",
            "descriptions_truncated",
            "catalog_char_limit",
            "catalog_included",
            "explicit_skill_ids",
            "unknown_skill_mentions",
            "omitted_explicit_skill_ids",
        ]:
            self.assertIn(field, retrieve_response["properties"])
        read_response = schema["components"]["schemas"]["ReadSkillContentResponse"]
        self.assertIn("next_start_line", read_response["properties"])

    def test_openapi_json_infers_or_uses_server_url(self) -> None:
        client = TestClient(create_app())
        response = client.get(
            "/openapi.json",
            headers={
                "x-forwarded-proto": "https",
                "x-forwarded-host": "skills.example.com",
            },
        )
        self.assertEqual(response.json()["servers"], [{"url": "https://skills.example.com"}])
        self.assertEqual(
            create_app(server_url="https://skills.example.com/api/").openapi()["servers"],
            [{"url": "https://skills.example.com/api"}],
        )

    def test_bearer_token_protects_action_endpoints(self) -> None:
        with patch.dict(os.environ, {"SKILL_TEMPLE_BEARER_TOKEN": "test-secret"}, clear=False):
            client = TestClient(create_app())

        schema = client.get("/openapi.json").json()
        self.assertEqual(
            schema["components"]["securitySchemes"]["BearerAuth"],
            {"type": "http", "scheme": "bearer"},
        )
        missing = client.post(
            "/v1/skills/read",
            json={"skill_id": "idapython", "path": "SKILL.md", "max_lines": 5},
        )
        self.assertEqual(missing.status_code, 401)
        authorized = client.post(
            "/v1/skills/read",
            headers={"Authorization": "Bearer test-secret"},
            json={"skill_id": "idapython", "path": "SKILL.md", "max_lines": 5},
        )
        self.assertEqual(authorized.status_code, 200)

    def test_http_endpoints_follow_progressive_disclosure(self) -> None:
        client = TestClient(create_app())
        discovery = client.post(
            "/v1/skills/retrieve",
            json={"query": "反编译 main 函数"},
        )
        self.assertEqual(discovery.status_code, 200)
        discovery_body = discovery.json()
        self.assertEqual(discovery_body["selected_skills"], [])
        self.assertEqual(
            discovery_body["decision"]["next_action"],
            "selectSkillOrAnswer",
        )
        self.assertIn("中文", discovery_body["available_skills"][0]["description"])

        retrieve = client.post(
            "/v1/skills/retrieve",
            json={
                "query": "反编译 main 函数",
                "hinted_skill_ids": ["idapython"],
            },
        )
        self.assertEqual(retrieve.status_code, 200)
        body = retrieve.json()
        selected = body["selected_skills"][0]
        self.assertIn("name: idapython", selected["instructions"])
        self.assertIn("docs/idautils.md", selected["referenced_paths"])
        self.assertEqual(body["decision"]["next_action"], "followSkillInstructions")

        search = client.post(
            "/v1/skills/search",
            json={"skill_id": "idapython", "query": "ctree_visitor_t cot_call"},
        )
        self.assertEqual(search.status_code, 200)
        self.assertEqual(search.json()["recommended_next_action"], "readSkillContent")
        path = search.json()["matches"][0]["path"]

        read = client.post(
            "/v1/skills/read",
            json={"skill_id": "idapython", "path": path},
        )
        self.assertEqual(read.status_code, 200)
        self.assertIn("ctree", read.json()["content"].lower())

        public_debug = client.post(
            "/v1/skills/retrieve",
            json={"query": "@idapython task", "include_debug": True},
        )
        self.assertEqual(public_debug.status_code, 422)

    def test_hidden_console_keeps_debug_and_api_timeline(self) -> None:
        client = TestClient(create_app())
        html = client.get("/console").text
        for term in [
            "API Call Timeline",
            "Bearer Token",
            "apiCall",
            "sessionStorage",
            "Authorization",
            "***redacted***",
            "executeIdapython",
        ]:
            self.assertIn(term, html)
        self.assertNotIn("max_docs", html)
        self.assertNotIn("fetch('/console/retrieve'", html)

        response = client.post(
            "/console/retrieve",
            json={
                "query": "@idapython find xrefs",
                "hinted_skill_ids": ["idapython"],
                "include_debug": True,
            },
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn("debug", response.json())

    def test_http_expected_errors_are_structured(self) -> None:
        client = TestClient(create_app())
        missing = client.post(
            "/v1/skills/read",
            json={"skill_id": "missing", "path": "SKILL.md"},
        )
        self.assertEqual(missing.status_code, 404)
        self.assertEqual(missing.json()["detail"]["error"]["code"], "skill_not_found")

        unsafe = client.post(
            "/v1/skills/read",
            json={"skill_id": "idapython", "path": "../README.md"},
        )
        self.assertEqual(unsafe.status_code, 404)
        self.assertEqual(unsafe.json()["detail"]["error"]["code"], "unsafe_or_missing_path")

        bad_hint = client.post(
            "/v1/skills/retrieve",
            json={"query": "@missing task", "hinted_skill_ids": ["missing"]},
        )
        self.assertEqual(bad_hint.status_code, 404)
        self.assertEqual(bad_hint.json()["detail"]["error"]["code"], "skill_not_found")

    def test_eval_file_passes_packaged_skill_queries(self) -> None:
        report = evaluate_file(Path("evals/skill_queries.jsonl"))
        self.assertEqual(report["failed"], 0)
        self.assertGreaterEqual(report["passed"], 2)


if __name__ == "__main__":
    unittest.main()
