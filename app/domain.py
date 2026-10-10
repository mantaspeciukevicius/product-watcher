import re, unicodedata, math
from urllib.parse import urlparse
from pydantic import BaseModel, Field, field_validator
HOSTS={'vinted':('vinted.lt',),'skelbiu':('skelbiu.lt',),'allegro':('allegro.pl','allegrolokalnie.pl')}
def normalized(s):
    s=s.lower().replace('ł','l')
    return ''.join(x for x in unicodedata.normalize('NFKD',s) if not unicodedata.combining(x))
class Listing(BaseModel):
    source: str
    external_id: str = Field(min_length=1,max_length=150)
    title: str = Field(min_length=1,max_length=500)
    description: str = Field(default='',max_length=30000)
    url: str = Field(max_length=2000)
    image: str = Field(default='',max_length=2000)
    price: float = Field(ge=0,le=1000000,allow_inf_nan=False)
    currency: str = 'EUR'
    shipping: float|None = Field(default=None,ge=0,le=100000,allow_inf_nan=False)
    fees: float|None = Field(default=None,ge=0,le=100000,allow_inf_nan=False)
    used: bool|None = None
    @field_validator('source')
    @classmethod
    def source_valid(cls,v):
        if v not in HOSTS: raise ValueError('Unknown marketplace')
        return v
    @field_validator('currency')
    @classmethod
    def currency_valid(cls,v):
        v=v.upper()
        if v not in ('EUR','PLN'): raise ValueError('Only EUR and PLN are supported')
        return v
    @field_validator('url')
    @classmethod
    def url_valid(cls,v,info):
        p=urlparse(v)
        hosts=HOSTS.get(info.data.get('source'),())
        if p.scheme!='https' or p.username or p.password or not any(p.hostname==h or (p.hostname or '').endswith('.'+h) for h in hosts): raise ValueError('Use an HTTPS link to the selected marketplace')
        return v
    @field_validator('image')
    @classmethod
    def image_valid(cls,v):
        if v and (urlparse(v).scheme!='https' or urlparse(v).username): raise ValueError('Photo must use HTTPS')
        return v

def classify(item):
    t=normalized(item.title+' '+item.description)
    if re.search(r'\b(pnv[\s-]?57e?|pvs[\s-]?2|gen[\s-]?0)\b',t): return 'excluded'
    if re.search(r'\b(fpv|thermal|termowiz\w*|termoviz\w*|therm\w*)\b',t): return 'excluded'
    if re.search(r'\b(?:generacj[ai]|kartos)\s*(?:1|i)\b',t): return 'excluded'
    if re.search(r'\b(gen(?:eration)?\s*[-:]?\s*(?:1|i)(?:\b|\+)|1\s*(?:kart\w*|generac\w*))',t): return 'excluded'
    # Accessories alone should never trigger a device alert.
    if re.search(r'\b(only|tik|tylko)\s+(mount|helmet|adapter|case|laikiklis|korpus)',t) or re.search(r'\b(dummy|replica|replika|atrapa)\b',t): return 'excluded'
    for model in ('30','50'):
        if re.search(r'\bnvg[\s-]?'+model+r'\b',t): return 'nvg'+model
    if not re.search(r'naktin\w*|noktow\w*|night[ -]?vision|intensifier|\bpvs[ -]?\d',t): return 'unclassified'
    if re.search(r'\b(digital|cyfrow\w*|skaitmenin\w*)\b',t): return 'excluded'
    if re.search(r'\b(?:generacj[ai]|kartos)\s*(?:2|ii)\s*\+',t): return 'gen2plus'
    if re.search(r'\b(gen(?:eration)?\s*[-:]?\s*(?:2|ii)\s*\+|2\s*\+\s*(?:kart\w*|generac\w*))',t): return 'gen2plus'
    if re.search(r'\b(?:generacj[ai]|kartos)\s*(?:2|ii)\b',t): return 'gen2'
    if re.search(r'\b(gen(?:eration)?\s*[-:]?\s*(?:2|ii)\b|2\s*(?:kart\w*|generac\w*))',t): return 'gen2'
    if re.search(r'\b(gen(?:eration)?\s*3|3\s*generac\w*)',t): return 'other'
    if re.search(r'\b(analog\w*|image intensifier|intensifier tube|fotokatod\w*|eop|pvs[\s-]?(?:7|14))\b',t): return 'analog_unknown'
    return 'unclassified'
def matches(item,category,total,s):
    if item.used is False or not s.get(item.source+'_enabled',True): return False
    return category in s['categories'] and total<s['max_price'] and any(normalized(k) in normalized(item.title+' '+item.description) for k in s['keywords'])
def total_eur(item,rate):
    amount=item.price+(item.shipping or 0)+(item.fees or 0)
    return round(amount/(rate if item.currency=='PLN' else 1),2)
