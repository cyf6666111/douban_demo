/* ==========================================================================
   index.js —— 外壳导航逻辑
   原项目的问题：
   1. href 写在 <dd> 上（dd 不是链接元素），只能靠 jQuery 读属性的"约定"，
      不能用键盘 Tab 聚焦、不能中键新标签页打开、右键也没有"复制链接"；
   2. 没有地址栏同步，刷新后总是跳回默认页，没法把某个图表页发给别人；
   3. 没有选中态与内容的同步（切换后旧的选中态不会清除）。
   现在：导航用真实 <a href>，键盘/中键/右键全部原生可用；
   通过 location.hash 记录当前页，刷新可复原；加载过程有细进度条反馈。
   ========================================================================== */

(function () {
    'use strict';

    var DEFAULT_PAGE = '/movie_list';

    document.addEventListener('DOMContentLoaded', function () {
        var frame = document.querySelector('.stage-frame');
        var bar = document.querySelector('.stage-bar');
        var links = Array.prototype.slice.call(document.querySelectorAll('.nav-item[href]'));
        if (!frame || !links.length) { return; }

        function markActive(href) {
            links.forEach(function (link) {
                var isActive = link.getAttribute('href') === href;
                link.classList.toggle('is-active', isActive);
                if (isActive) {
                    link.setAttribute('aria-current', 'page');
                } else {
                    link.removeAttribute('aria-current');
                }
            });
        }

        function show(href, pushHash) {
            if (!href) { return; }
            markActive(href);
            if (bar) {
                bar.classList.add('is-loading');
                bar.classList.remove('is-done');
            }
            frame.setAttribute('src', href);
            // 用 replaceState 记录，避免每次切页都在浏览器历史里插一条
            if (pushHash !== false && window.history && window.history.replaceState) {
                window.history.replaceState(null, '', '#' + href);
            }
        }

        links.forEach(function (link) {
            link.addEventListener('click', function (event) {
                // 让 Ctrl/Cmd/Shift/中键点击走浏览器默认行为（新标签页打开）
                if (event.metaKey || event.ctrlKey || event.shiftKey || event.button !== 0) { return; }
                event.preventDefault();
                show(link.getAttribute('href'));
            });
        });

        // iframe 载入完成：进度条收尾
        frame.addEventListener('load', function () {
            if (!bar) { return; }
            bar.classList.remove('is-loading');
            bar.classList.add('is-done');
            window.setTimeout(function () { bar.classList.remove('is-done'); }, 420);
        });

        // 从地址栏 hash 还原页面，支持直接分享 /#/movie_top 这样的链接
        var initial = (window.location.hash || '').replace(/^#/, '');
        var known = links.some(function (link) { return link.getAttribute('href') === initial; });
        show(known ? initial : DEFAULT_PAGE, false);
    });
})();
