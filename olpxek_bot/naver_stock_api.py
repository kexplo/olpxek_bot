from abc import ABCMeta, abstractmethod
from dataclasses import dataclass, field
from typing import Dict, List, Optional, TypedDict

from asyncache import cached
from bs4 import BeautifulSoup
from cachetools import LRUCache
import httpx


class InvalidStockQuery(Exception):
    pass


@dataclass
class NaverStockMetadata:
    symbol_code: str
    display_name: str
    stock_exchange_name: str
    url: str
    reuters_code: str
    is_etf: bool = field(init=False)
    is_global: bool = field(init=False)

    def __post_init__(self):
        self.is_etf = 'etf' in self.url
        self.is_global = self.stock_exchange_name not in ['코스피', '코스닥']


@dataclass
class NaverStockData:
    name: str
    name_eng: Optional[str]
    symbol_code: str
    close_price: str
    stock_exchange_name: str
    compare_price: str
    compare_ratio: str
    total_infos: Dict[str, str]  # TODO: ETF랑 Stock이랑 별도로 정의하면 좋겠다
    image_chart_types: List[str]
    image_charts: Dict[str, str]
    day_graph_url: str = field(init=False)
    candle_graph_url: str = field(init=False)
    url: str = field(init=False)

    def __post_init__(self):
        if self.compare_price[0] != '-':
            self.compare_price = '🔺' + self.compare_price
        if self.compare_ratio[0] != '-':
            self.compare_ratio = '🔺' + self.compare_ratio
        self.compare_ratio += '%'

        # NOTE: 국내 주식 정보와 해외 주식 정보의 데이터 양식이 다르다
        if 'day' in self.image_charts:
            self.day_graph_url = self.image_charts['day']
        else:
            self.day_graph_url = self.image_charts['1일']
        if 'candleMonth' in self.image_charts:
            self.candle_graph_url = self.image_charts['candleMonth']
        else:
            self.candle_graph_url = self.image_charts['일봉']


class NaverStockAPIResponse(TypedDict):
    stockName: str  # noqa: N815
    stockNameEng: str  # noqa: N815
    symbolCode: str  # noqa: N815
    closePrice: str  # noqa: N815
    stockExchangeType: Dict[str, str]  # noqa: N815
    compareToPreviousClosePrice: str  # noqa: N815
    stockItemTotalInfos: List[Dict[str, str]]  # noqa: N815
    fluctuationsRatio: str  # noqa: N815
    imageChartTypes: List[str]  # noqa: N815
    imageCharts: Dict[str, str]  # noqa: N815


class NaverStockAPIParser(metaclass=ABCMeta):
    def __init__(self, stock_metadata: NaverStockMetadata):
        self.metadata = stock_metadata

    @abstractmethod
    async def _get_stock_data_impl(self) -> NaverStockData:
        pass

    async def get_stock_data(self) -> NaverStockData:
        stock_data = await self._get_stock_data_impl()
        stock_data.url = self.metadata.url
        return stock_data

    @classmethod
    def api_response_to_stock_data(
        cls,
        response: NaverStockAPIResponse
    ) -> NaverStockData:
        total_infos = {}  # type: Dict[str, str]
        for total_info in response['stockItemTotalInfos']:
            total_infos[total_info['key']] = total_info['value']
            # code, key, value[,
            #   compareToPreviousPrice[code(2,5), text(상승,하락), name]]

        return NaverStockData(
            response['stockName'],
            response['stockNameEng'],
            response['symbolCode'],
            response['closePrice'],
            response['stockExchangeType']['name'],
            response['compareToPreviousClosePrice'],
            response['fluctuationsRatio'],
            total_infos,
            response['imageChartTypes'],
            response['imageCharts']
        )


class NaverStockAPIGlobalETFParser(NaverStockAPIParser):
    async def _get_stock_data_impl(self) -> NaverStockData:
        json_dict = None
        async with httpx.AsyncClient() as client:
            r = await client.get(
                f'https://api.stock.naver.com/etf/'
                f'{self.metadata.reuters_code}/basic')
            json_dict = r.json()
        return self.api_response_to_stock_data(json_dict)


class NaverStockAPIGlobalStockParser(NaverStockAPIParser):
    async def _get_stock_data_impl(self) -> NaverStockData:
        json_dict = None
        async with httpx.AsyncClient() as client:
            r = await client.get(
                f'https://api.stock.naver.com/stock/'
                f'{self.metadata.reuters_code}/basic')
            json_dict = r.json()
        return self.api_response_to_stock_data(json_dict)


class NaverStockAPIKoreaStockParser(NaverStockAPIParser):
    async def _get_stock_data_impl(self) -> NaverStockData:
        code = self.metadata.symbol_code
        async with httpx.AsyncClient() as client:
            r = await client.get(
                'https://m.stock.naver.com/api/item/getOverallHeaderItem.nhn'
                f'?code={code}')
            header_json = r.json()['result']

        async with httpx.AsyncClient() as client:
            r = await client.get(
                'https://m.stock.naver.com/api/html/item/getOverallInfo.nhn'
                f'?code={code}')
            html = r.text

        name = header_json['nm']
        time = header_json['time']
        symbol_code = header_json['cd']
        close_price = f'{header_json["nv"]:,}'
        compare_price = f'{header_json["cv"]:,}'
        compare_ratio = f'{header_json["cr"]:,}'
        stock_exchange_name = self.metadata.stock_exchange_name

        soup = BeautifulSoup(html, 'html.parser')
        total_info_lis = soup.select('ul.total_lst > li')
        total_infos = {
            li.find('div').text.strip(): li.find('span').text.strip()
            for li in total_info_lis}

        image_chart_types = [li.find('span').text.strip()
                             for li in soup.select('ul.lnb_lst > li')]
        # 클라이언트에서 이미지 캐시되는 것을 막기 위해
        # 유효하지 않지만 URL 뒤에 '?time' 인자를 덧붙인다
        charts = [img.attrs['data-src'] + f'?{time}'
                  for img in soup.select('div.flick-ct * > img')]
        image_charts = {img_type: chart
                        for img_type, chart in zip(image_chart_types, charts)}
        return NaverStockData(
            name,
            None,
            symbol_code,
            close_price,
            stock_exchange_name,
            compare_price,
            compare_ratio,
            total_infos,
            image_chart_types,
            image_charts
        )


class NaverStockAPIParserFactory(object):
    @classmethod
    def from_metadata(cls, stock_metadata: NaverStockMetadata):
        if stock_metadata.is_global:
            if stock_metadata.is_etf:
                return NaverStockAPIGlobalETFParser(stock_metadata)
            return NaverStockAPIGlobalStockParser(stock_metadata)
        # kospi, kosdaq
        return NaverStockAPIKoreaStockParser(stock_metadata)


class NaverStockAPI(object):
    @classmethod
    async def from_query(cls, query: str):
        metadata = await cls.get_metadata(query)
        return NaverStockAPI(metadata)

    @classmethod
    @cached(LRUCache(maxsize=20))
    async def get_metadata(cls, query: str) -> NaverStockMetadata:
        url_tmpl = 'https://ac.finance.naver.com/ac?q={query}&q_enc=euc-kr&t_koreng=1&st=111&r_lt=111'  # noqa: E501
        async with httpx.AsyncClient() as client:
            r = await client.get(url_tmpl.format(query=query))
            try:
                json_dict = r.json()
                first_item = json_dict['items'][0][0]
            except IndexError:
                raise InvalidStockQuery(json_dict)
            symbol_code, display_name, market, url, reuters_code = first_item
            # NOTE: 모든 값이 list로 감싸져있다
        return NaverStockMetadata(symbol_code[0], display_name[0], market[0],
                                  f'https://m.stock.naver.com{url[0]}',
                                  reuters_code[0])

    def __init__(self, metadata: NaverStockMetadata):
        self.metadata = metadata
        self.parser = NaverStockAPIParserFactory.from_metadata(metadata)

    async def get_stock_data(self) -> NaverStockData:
        return await self.parser.get_stock_data()