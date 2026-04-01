"""
DuckDuckGo搜索工具 - DeerFlow内置工具
"""

import logging
from typing import Optional

from langchain_community.tools import DuckDuckGoSearchRun
from langchain_community.utilities import DuckDuckGoSearchAPIWrapper
from langchain_core.tools import BaseTool

logger = logging.getLogger(__name__)


def create_duckduckgo_search_tool(max_results: int = 3, region: str = "wt-wt", time_range: Optional[str] = None, name: str = "duckduckgo_search", description: Optional[str] = None) -> BaseTool:
    """
    创建DuckDuckGo搜索工具
    """
    wrapper = DuckDuckGoSearchAPIWrapper(region=region, time=time_range, max_results=max_results)

    tool = DuckDuckGoSearchRun(api_wrapper=wrapper)
    tool.name = name
    tool.description = description or ("A privacy-focused search engine. Use this when you need to search the web for current events, facts, information, or research topics. Input should be a clear search query.")

    logger.info(f"Created DuckDuckGo search tool (region={region}, max_results={max_results})")
    return tool


# 标准搜索工具
duckduckgo_search_tool = create_duckduckgo_search_tool(max_results=3, region="wt-wt")

# 中文搜索工具
duckduckgo_search_chinese_tool = create_duckduckgo_search_tool(max_results=5, region="cn-zh", name="duckduckgo_search_chinese", description=("中文网络搜索引擎。使用此工具搜索中文网页内容、中国相关的新闻和信息。输入应该是中文搜索词。"))

# 所有DuckDuckGo工具列表
DUCKDUCKGO_TOOLS = [
    duckduckgo_search_tool,
    duckduckgo_search_chinese_tool,
]
