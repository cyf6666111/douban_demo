/* ==========================================================================
   dashboard.js —— 图表运行时（所有图表页共用）
   原项目的问题：6 个模板各自复制一遍 $.ajax + echarts.init + setOption，
   没有错误处理、没有 resize、没有载入提示，改一处要改六处。
   现在统一到这一个文件：<div class="chart" data-chart-url="..."> 即可渲染。

   为什么去掉 jQuery？
   这里只需要"GET 一个 JSON 并渲染"，原生 fetch 完全够用；
   而 jQuery 3.1.1（原项目自带）体积 ~90KB 且存在已公开的 XSS 漏洞
   （CVE-2020-11022 / CVE-2020-11023，3.5.0 才修复）。
   去掉后前端除 ECharts 外零第三方依赖。
   ========================================================================== */

(function () {
    'use strict';

    /* ---------- 与 CSS 变量一致的 ECharts 主题 ---------- */
    var PALETTE = ['#E0A33E', '#3E9B63', '#C0554B', '#5B7FA6', '#A8762A',
                   '#7C6BA8', '#4E8F8A', '#B5714A', '#6E8B4F', '#9C5B7C', '#8C8578'];

    var MONO = "'Cascadia Mono', Consolas, 'Microsoft YaHei', monospace";
    var TEXT = '#2B2620';
    var SOFT = '#6C6455';
    var LINE = '#EAE4D7';
    var AXIS = '#CDC2AC';

    var THEME = {
        color: PALETTE,
        backgroundColor: 'transparent',
        textStyle: { fontFamily: MONO, color: SOFT },
        animationDuration: 620,
        animationEasing: 'cubicOut',
        grid: { left: 12, right: 20, top: 30, bottom: 10, containLabel: true },
        tooltip: {
            backgroundColor: 'rgba(16,15,13,.94)',
            borderWidth: 0,
            padding: [9, 12],
            textStyle: { color: '#F2EDE2', fontSize: 12, fontFamily: MONO },
            extraCssText: 'border-radius:2px;box-shadow:0 10px 28px -12px rgba(0,0,0,.55);'
        },
        legend: {
            textStyle: { color: SOFT, fontSize: 11, fontFamily: MONO },
            itemWidth: 10,
            itemHeight: 10
        },
        categoryAxis: {
            axisLine: { lineStyle: { color: AXIS } },
            axisTick: { show: false },
            axisLabel: { color: SOFT, fontSize: 11, fontFamily: MONO },
            splitLine: { show: false }
        },
        valueAxis: {
            axisLine: { show: false },
            axisTick: { show: false },
            axisLabel: { color: SOFT, fontSize: 11, fontFamily: MONO },
            splitLine: { lineStyle: { color: LINE, type: 'dashed' } }
        },
        toolbox: {
            iconStyle: { borderColor: SOFT },
            emphasis: { iconStyle: { borderColor: '#A8762A' } }
        }
    };

    if (window.echarts) {
        window.echarts.registerTheme('archive', THEME);
    }

    /* ---------- 工具函数 ---------- */
    function buildUrl(url, params) {
        if (!params) { return url; }
        var parts = [];
        Object.keys(params).forEach(function (key) {
            var value = params[key];
            if (value !== undefined && value !== null && value !== '') {
                parts.push(encodeURIComponent(key) + '=' + encodeURIComponent(value));
            }
        });
        if (!parts.length) { return url; }
        return url + (url.indexOf('?') === -1 ? '?' : '&') + parts.join('&');
    }

    function stateEl(container) {
        var frame = container.parentNode;
        var el = frame && frame.querySelector('.chart-state');
        return el;
    }

    function showLoading(container) {
        var el = stateEl(container);
        if (!el) { return; }
        el.hidden = false;
        el.className = 'chart-state';
        el.innerHTML = '<span class="filmstrip"><i></i><i></i><i></i></span><span>数据载入中…</span>';
    }

    function showError(container, message) {
        var el = stateEl(container);
        if (!el) { return; }
        el.hidden = false;
        el.className = 'chart-state chart-error';
        el.innerHTML =
            '<span class="title">图表加载失败</span>' +
            '<code>' + String(message).replace(/[<>&]/g, '') + '</code>' +
            '<code>请检查服务是否运行、数据库是否可连接</code>';
    }

    function hideState(container) {
        var el = stateEl(container);
        if (el) { el.hidden = true; }
    }

    /* ---------- 渲染单个图表 ---------- */
    function renderChart(container) {
        var url = container.getAttribute('data-chart-url');
        if (!url) { return; }

        var params = null;
        var rawParams = container.getAttribute('data-chart-params');
        if (rawParams) {
            try {
                params = JSON.parse(rawParams);
            } catch (err) {
                showError(container, '图表参数格式错误');
                return;
            }
        }

        showLoading(container);

        fetch(buildUrl(url, params), { headers: { Accept: 'application/json' } })
            .then(function (response) {
                return response.json().catch(function () { return null; })
                    .then(function (body) { return { ok: response.ok, status: response.status, body: body }; });
            })
            .then(function (result) {
                if (!result.ok || !result.body || result.body.error) {
                    var msg = (result.body && result.body.error) || ('HTTP ' + result.status);
                    throw new Error(msg);
                }
                var chart = window.echarts.init(container, 'archive', { renderer: 'canvas' });
                chart.setOption(result.body, true);
                hideState(container);
                watchResize(container, chart);
                // 供外部（如打印、截图）取用
                container.__chart = chart;
            })
            .catch(function (err) {
                if (window.console) { window.console.error('[chart]', url, err); }
                showError(container, err.message || '未知错误');
            });
    }

    /* ---------- 尺寸自适应 ----------
       iframe 内 window.resize 不一定触发（宿主改变 iframe 高度时），
       因此优先使用 ResizeObserver 监听容器本身。 */
    function watchResize(container, chart) {
        if (typeof ResizeObserver === 'function') {
            var observer = new ResizeObserver(function () { chart.resize(); });
            observer.observe(container);
            container.__resizeObserver = observer;
        } else {
            window.addEventListener('resize', function () { chart.resize(); });
        }
    }

    /* ---------- 自动初始化 ---------- */
    function initAll() {
        if (!window.echarts) {
            document.querySelectorAll('.chart[data-chart-url]').forEach(function (el) {
                showError(el, 'ECharts 未加载');
            });
            return;
        }
        document.querySelectorAll('.chart[data-chart-url]').forEach(renderChart);
    }

    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', initAll);
    } else {
        initAll();
    }

    /* 暴露给需要手动渲染的页面 */
    window.Dashboard = { render: renderChart, init: initAll, theme: THEME };
})();
