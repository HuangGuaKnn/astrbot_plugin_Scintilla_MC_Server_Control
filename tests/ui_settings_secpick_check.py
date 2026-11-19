"""唯一可视化颜色卡的格式化色回归（真浏览器）。

v0.30.0 移除旧手动卡后，用现存的 16 色名选择器和文本框验证同一能力：
三项独立、§码/hex 反向对位、未知值不被改写，以及保存全部设置的实际载荷。
"""
from __future__ import annotations

from playwright.sync_api import expect, sync_playwright

from _paths import UI_LAUNCH_KWARGS
from _ui_page import PAGE_URL, VIEWPORT
from ui_player_admin_check import Scenario

PAIRS = [("clr_say_txt", "clr_say_sel"),
         ("clr_title_txt", "clr_title_sel"),
         ("clr_fb_txt", "clr_fb_sel")]


def main() -> int:
    with sync_playwright() as pw:
        browser = pw.chromium.launch(**UI_LAUNCH_KWARGS)
        page = browser.new_page(viewport=VIEWPORT)
        scenario = Scenario(page)
        scenario.color_config = {
            "color_say": "white", "color_title": "gold", "color_feedback": "gold",
            "gradient_enabled": False, "gradient_format": "json", "plain_color_format": "hex",
            "gradient_colors_say": "#FFFFFF,#55FFFF", "gradient_colors_title": "#FFAA00,#FF00FF",
            "gradient_colors_feedback": "#FFAA00,#55FF55",
        }
        page.goto(PAGE_URL)
        page.locator('button.tab[data-page="settings"]').click()
        expect(page.locator("#clr_say_txt")).to_have_value("white")
        expect(page.locator("#clr_title_txt")).to_have_value("gold")
        expect(page.locator("#clr_fb_txt")).to_have_value("gold")

        print("[1] 单卡、三对控件齐全、初值按接口回显")
        assert page.locator("#page_settings #srv_color_card").count() == 1
        assert page.locator('[id^="cfg_color_"], #cfg_plain_fmt, #cfg_grad_fmt').count() == 0
        assert page.locator("#page_settings input[type=color]:visible").count() == 3
        assert page.locator("#page_settings input[type=color]").count() \
            == page.locator("#srv_color_card input[type=color]").count()
        for text_id, select_id in PAIRS:
            assert page.locator(f"#srv_color_card #{text_id}, #srv_color_card #{select_id}").count() == 2

        print("[2] 固定格式化色模式：三项可见，16 色按 §0-§f 排列，标签含符号/中文/色名")
        page.select_option("#clr_plain_fmt", "named")
        names = ["black", "dark_blue", "dark_green", "dark_aqua", "dark_red", "dark_purple", "gold",
                 "gray", "dark_gray", "blue", "green", "aqua", "red", "light_purple", "yellow", "white"]
        for text_id, select_id in PAIRS:
            selector = page.locator(f"#{select_id}")
            expect(selector).to_be_visible()
            expect(page.locator(f"#{text_id}")).to_be_hidden()
            assert selector.locator("option").evaluate_all("els => els.map(el => el.value)") == names
            labels = selector.locator("option").all_text_contents()
            assert "§6" in labels[6] and "金" in labels[6] and "gold" in labels[6]
            assert "§f" in labels[-1] and "white" in labels[-1]
        expect(page.locator("#clr_say_sel")).to_have_value("white")
        expect(page.locator("#clr_title_sel")).to_have_value("gold")
        expect(page.locator("#clr_fb_sel")).to_have_value("gold")

        print("[3] 选择格式化色更新文本值与预览，三项互不影响")
        page.select_option("#clr_say_sel", "red")
        expect(page.locator("#clr_say_txt")).to_have_value("red")
        expect(page.locator("#clr_say")).to_have_value("#ff5555")
        expect(page.locator("#clr_title_txt")).to_have_value("gold")
        expect(page.locator("#clr_fb_txt")).to_have_value("gold")

        print("[4] 手输 hex / §码与色名对位，未知 RGB 文本不被改写")
        page.select_option("#clr_plain_fmt", "hex")
        page.fill("#clr_title_txt", "#FFAA00")
        expect(page.locator("#clr_title_sel")).to_have_value("gold")
        page.fill("#clr_title_txt", "&c")
        expect(page.locator("#clr_title_sel")).to_have_value("red")
        page.fill("#clr_fb_txt", "RGB(255,0,0)")
        expect(page.locator("#clr_fb_txt")).to_have_value("RGB(255,0,0)")
        page.fill("#clr_say_txt", "§9")
        expect(page.locator("#clr_say_sel")).to_have_value("blue")
        expect(page.locator("#clr_title_txt")).to_have_value("&c")
        expect(page.locator("#clr_fb_txt")).to_have_value("RGB(255,0,0)")
        page.select_option("#clr_plain_fmt", "named")
        page.select_option("#clr_plain_fmt", "hex")
        expect(page.locator("#clr_fb_txt")).to_have_value("RGB(255,0,0)")

        print("[5] 保存全部设置提交唯一草稿，§码原值和另两格都不丢")
        with page.expect_response("**/settings/save"):
            page.locator("#btn_save_cfg_top").click()
        assert scenario.settings_saves, "未发出保存全部设置请求"
        saved = scenario.settings_saves[-1]
        assert saved["color_say"] == "§9" and saved["color_title"] == "&c" \
            and saved["color_feedback"] == "RGB(255,0,0)", saved
        assert saved["plain_color_format"] == "hex"
        assert not scenario.errors, f"页面 JS 异常：{scenario.errors}"
        browser.close()
    print("[PASS] 唯一颜色卡：格式化色、双向对位、独立编辑、未知值保留、真实保存载荷")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
