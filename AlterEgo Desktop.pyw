# AlterEgo Desktop v10.3 — Python 3.13+ / PySide6
# Read-only WoW SavedVariables viewer. Local preferences stored separately.
import sys, os, re, json, traceback, weakref, hashlib, subprocess, tempfile, urllib.parse, time
from pathlib import Path
from datetime import datetime
from html import escape, unescape
from PySide6.QtCore import Qt, QTimer, QPoint, QRect, QSize, Signal, QEvent, QSaveFile, QIODevice
from PySide6.QtGui import QIcon, QColor, QPainter, QFont, QBrush, QPen, QPalette, QPixmap, QCursor, QRadialGradient
from PySide6.QtNetwork import QNetworkAccessManager, QNetworkRequest, QNetworkReply
from PySide6.QtCore import QUrl
from PySide6.QtWidgets import (QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QPushButton, QLabel, QLineEdit, QComboBox, QTableWidget, QTableWidgetItem,
    QTabWidget, QFileDialog, QMessageBox, QSplitter, QHeaderView, QInputDialog,
    QMenu, QAbstractItemView, QFrame, QScrollArea, QCheckBox, QDialog, QDialogButtonBox, QGridLayout, QToolButton, QGroupBox, QFormLayout, QToolTip, QSizePolicy, QSpinBox, QListView, QProgressDialog)

APP_DIR = Path(os.environ.get('LOCALAPPDATA', str(Path.home()))) / 'AlterEgoDesktop'
SETTINGS = APP_DIR / 'settings.json'
APP_VERSION = '1.0.8'
# Set before publishing: e.g. 'owner/AlterEgo-Desktop'. No personal data is sent.
UPDATE_REPOSITORY = 'Forelingh/AlterEgo-Desktop'

def obj(v): return v if isinstance(v, dict) else {}
def arr(v): return v if isinstance(v, list) else []
def fmt(v): return str(v) if v is not None else '—'

# Safe Lua SavedVariables subset parser. Does NOT execute Lua code.
TOKEN = re.compile(r'''\s+|--\[\[.*?\]\]|--[^\n]*|"(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*'|\[|\]|\{|\}|=|,|;|[-+]?(?:\d+\.\d*|\d*\.\d+|\d+)(?:[eE][-+]?\d+)?|[A-Za-z_][A-Za-z_0-9]*''', re.S)
class LuaReader:
    def __init__(self, source):
        self.tokens = [m.group() for m in TOKEN.finditer(source) if not m.group().isspace() and not m.group().startswith('--')]
        self.i = 0
    def peek(self): return self.tokens[self.i] if self.i < len(self.tokens) else None
    def take(self):
        x=self.peek(); self.i+=1; return x
    def expect(self,x):
        if self.take()!=x: raise ValueError('Некорректный формат Lua: ожидалось '+x)
    def value(self):
        x=self.take()
        if x is None: raise ValueError('Неожиданный конец файла')
        if x=='{':
            d={}; index=1
            while self.peek()!='}':
                if self.peek() is None: raise ValueError('Не закрыта таблица Lua')
                if self.peek()=='[':
                    self.take(); k=self.value(); self.expect(']'); self.expect('='); d[k]=self.value()
                elif self.i+1<len(self.tokens) and self.tokens[self.i+1]=='=':
                    k=self.take(); self.take(); d[k]=self.value()
                else:
                    d[index]=self.value(); index+=1
                if self.peek() in (',',';'): self.take()
            self.take()
            if d and all(isinstance(k,int) and k>0 for k in d) and set(d)==set(range(1,len(d)+1)):
                return [d[k] for k in range(1,len(d)+1)]
            return d
        if x[0] in ('"',"'"):
            # WoW strings use standard Lua escape sequences; preserve unknown escapes.
            body=x[1:-1]
            return re.sub(r'\\(\d{1,3}|.)', lambda m: chr(int(m[1])) if m[1].isdigit() else {'n':'\n','r':'\r','t':'\t','\\':'\\','"':'"',"'":"'"}.get(m[1],m[1]),body)
        if x in ('true','false','nil'): return {'true':True,'false':False,'nil':None}[x]
        try: return float(x) if any(c in x for c in '.eE') else int(x)
        except ValueError: return x

def load_lua(path):
    source=Path(path).read_text(encoding='utf-8-sig')
    p=LuaReader(source)
    if p.take()!='AlterEgoDB': raise ValueError('В файле не найден AlterEgoDB')
    p.expect('=')
    return obj(p.value())

def default_roots():
    guesses=[]
    for drive in [os.environ.get('SystemDrive','C:')]+[f'{chr(i)}:' for i in range(67,91)]:
        for suffix in ('Program Files (x86)/World of Warcraft/_retail_/WTF/Account',
                       'Program Files/World of Warcraft/_retail_/WTF/Account',
                       'World of Warcraft/_retail_/WTF/Account',
                       'Games/World of Warcraft/_retail_/WTF/Account'):
            p=Path(drive+'/')/suffix
            if p.is_dir(): guesses.append(str(p))
    return list(dict.fromkeys(guesses))

def find_files(root):
    p=Path(root)
    if p.is_file() and p.name.lower()=='alterego.lua': return [p]
    if not p.is_dir(): return []
    # Bound the search to the expected SavedVariables hierarchy, avoiding broad disk scans.
    candidates=[p/'AlterEgo.lua',p/'SavedVariables'/'AlterEgo.lua',p/'WTF'/'Account',p/'_retail_'/'WTF'/'Account']
    for c in candidates:
        if c.is_file(): return [c]
    account_root=next((c for c in candidates[2:] if c.is_dir()),p)
    files=[]
    for sub in account_root.iterdir():
        if sub.is_dir():
            f=sub/'SavedVariables'/'AlterEgo.lua'
            if f.is_file(): files.append(f)
    return files


RAID_NAMES = ['The Tidebound Grotto','The Venomous Abyss',"The Unbinding of Kith'ix","March on Quel'Danas"]
RAID_BOSSES = [
 ['Nymrissa Wavecaller'],
 ["Nek'zali the Soulcoiler",'Entombed Sentinels','The Lost Explorers','Vashnik the Malignant','Sszorak','The Twin Fangs','The Coiled Altar',"Ula'tek"],
 ["Kith'ix"],
 ["Belo'ren, Child of Al'ar","Midnight Falls"]]
DIFFS=[('Normal','#4ccf73'),('Heroic','#b369e6'),('Mythic','#df8b24')]

def raid_instance(c,raid,difficulty):
    # SavedVariables also keeps old, *unlocked* raid entries. AlterEgo does
    # not count those as this week's kills. Mythic raids may use difficulty
    # ID 233 ("Mythic - Flexible Raiding"), not only ID 16 ("Mythic").
    difficulty_ids = {
        'Normal': {14},
        'Heroic': {15},
        'Mythic': {16, 233},
    }
    valid_ids = difficulty_ids.get(difficulty, set())
    candidates = []
    for x in arr(obj(obj(c['data']).get('raids')).get('savedInstances')):
        if not isinstance(x, dict) or not x.get('isRaid') or (x.get('name') != raid and not (raid == "March on Quel'Danas" and str(x.get('instanceID')) == '2913')):
            continue
        if x.get('locked') is not True:
            continue
        # AlterEgo retains locked savedInstances even after weekly reset.
        # 'expires' is a Unix timestamp (seconds), not an active-lockout flag.
        # Ignore expired records so last week's kills do not carry over.
        try:
            expires = float(x.get('expires') or 0)
        except (TypeError, ValueError):
            continue
        if expires <= time.time():
            continue
        try:
            diff_id = int(x.get('difficultyID', -1))
        except (TypeError, ValueError):
            continue
        if diff_id not in valid_ids:
            continue
        candidates.append(x)
    if not candidates:
        return {}
    # Prefer the newest active lockout if there are duplicate records.
    return max(candidates, key=lambda x: (int(x.get('expires') or 0), int(x.get('id') or 0)))

def raid_flags(c,difficulty):
    out=[]
    for rn,bosses in zip(RAID_NAMES,RAID_BOSSES):
        inst=raid_instance(c,rn,difficulty)
        entries={str(x.get('bossName','')).casefold():bool(x.get('isKilled')) for x in arr(inst.get('encounters')) if isinstance(x,dict)}
        for boss in bosses:
            # Encounter index is a reliable fallback for localized/renamed bosses.
            match=next((x for x in arr(inst.get('encounters')) if isinstance(x,dict) and x.get('index')==bosses.index(boss)+1),{})
            out.append(entries.get(boss.casefold(),bool(match.get('isKilled'))))
    return out

def raid_tooltip(c,difficulty):
    from html import escape
    s=['<div style="font-size:13px;color:#eee"><b>Raid Progress</b><br><span style="color:#ffdd2d">Difficulty:</span> '+escape(difficulty)]
    flags=raid_flags(c,difficulty); k=0
    for raid,bosses in zip(RAID_NAMES,RAID_BOSSES):
        s.append('<div style="height:10px"></div><span style="color:#ffdd2d">'+escape(raid)+'</span><br>')
        for boss in bosses:
            s.append('<span style="color:'+('#48d76a' if flags[k] else '#888888')+'">'+escape(boss)+'</span><br>');k+=1
    s.append('</div>')
    return ''.join(s)

# Use the entire character column for raid progress. Both the raid labels
# and skulls use the same coordinates, recalculated on widget resize.
RAID_GAP = 11.0
SKULL_WIDTH = 255
CHAR_COLUMN_WIDTH = 270

def skull_x(i, width):
    # Leave a symmetrical margin at both edges; three internal raid gaps.
    left = 7.0
    right = 7.0
    skull_size = 15.0
    step = max(17.5, (width - left - right - skull_size - 3 * RAID_GAP) / 11.0)
    return left + i * step + (RAID_GAP if i >= 1 else 0) + (RAID_GAP if i >= 9 else 0) + (RAID_GAP if i >= 10 else 0)

def divider_x(boundary, width):
    return (skull_x(boundary-1, width) + 15 + skull_x(boundary, width)) / 2

class Skulls(QWidget):
    def __init__(self,c,difficulty,parent=None):
        super().__init__(parent)
        self.c=c;self.difficulty=difficulty
        self.setMinimumWidth(SKULL_WIDTH)
        self.setFixedHeight(26)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.tooltip_html='<div style="font-weight:700">'+raid_tooltip(c,difficulty)+'</div>'
        self.row_bg='#1b1e21'
        self.hovered=False
        self.setMouseTracking(True)

    def event(self,event):
        if event.type()==QEvent.Type.Enter:
            self.hovered=True;self.update()
            QToolTip.showText(self.mapToGlobal(QPoint(self.width()+8,0)),self.tooltip_html,self)
        elif event.type()==QEvent.Type.Leave:
            self.hovered=False;self.update()
            QToolTip.hideText()
        elif event.type()==QEvent.Type.ToolTip:
            QToolTip.showText(self.mapToGlobal(QPoint(self.width()+8,0)),self.tooltip_html,self)
            return True
        return super().event(event)

    def paintEvent(self,event):
        p=QPainter(self)
        p.fillRect(self.rect(),QColor('#29404a' if self.hovered else self.row_bg))
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        color=dict(DIFFS)[self.difficulty]
        flags=raid_flags(self.c,self.difficulty)
        for i,flag in enumerate(flags):
            x=skull_x(i,self.width());y=3
            p.setPen(QPen(QColor(color if flag else '#46505a'),1.4))
            p.setBrush(QBrush(QColor(color if flag else '#46505a')))
            p.drawEllipse(QRect(int(x+2),y,11,11))
            p.drawRect(QRect(int(x+4),y+9,7,4))
            p.setPen(QColor('#111a1c'))
            p.drawEllipse(QRect(int(x+4),y+4,2,2));p.drawEllipse(QRect(int(x+9),y+4,2,2))
        p.setRenderHint(QPainter.RenderHint.Antialiasing,False)
        p.setPen(QPen(QColor('#879198'),1))
        for boundary in (1,9,10):
            x=round(divider_x(boundary,self.width()))
            p.drawLine(x,3,x,19)

def season_currency_capped(currency, maximum_override=None):
    """True only when a known positive seasonal cap has actually been reached."""
    if not isinstance(currency,dict):return False
    earned=currency.get('totalEarned')
    if earned is None:earned=currency.get('quantityEarnedThisWeek')
    maximum=currency.get('maxWeeklyQuantity')
    try:
        if maximum is None or int(maximum) <= 0:
            maximum=currency.get('maxQuantity')
    except (TypeError,ValueError):
        maximum=currency.get('maxQuantity')
    if maximum_override is not None:
        maximum=maximum_override
    try:
        earned=int(earned)
        maximum=int(maximum)
        return maximum>0 and earned>=maximum
    except (TypeError,ValueError):
        return False

class HoverCurrency(QLabel):
    """Immediate tooltip and subtle full-cell highlight, without Qt's default delay."""
    def __init__(self, text, details, bg, parent=None, text_color=None):
        super().__init__(str(text), parent)
        self.details=details
        self.bg=bg
        self.setFixedHeight(26)
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.setContentsMargins(7,2,5,2)
        self.setMouseTracking(True)
        self.hovered=False
        self.setStyleSheet('color:'+(text_color or '#e8e8e8')+';font-weight:650;')

    def set_currency_icon(self,currency_id):
        if not currency_id or self.text() in ('–','—','-'):
            return
        self.setAlignment(Qt.AlignmentFlag.AlignVCenter|Qt.AlignmentFlag.AlignLeft)
        self._currency_icon=QLabel(self)
        self._currency_icon.setFixedSize(19,19)
        self._currency_icon.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._currency_icon.setStyleSheet('background:transparent;border:none;')
        self._currency_icon.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents,True)
        table_icons().request_currency(currency_id,self._currency_icon)
        self._position_currency_icon()
        self._currency_icon.show()

    def _position_currency_icon(self):
        icon=getattr(self,'_currency_icon',None)
        if icon is None:return
        # One fixed icon column for every currency row; values begin to its right.
        icon.move(max(2,self.width()//2-30),(self.height()-19)//2)
        self.setContentsMargins(max(7,self.width()//2-6),2,5,2)

    def resizeEvent(self,event):
        super().resizeEvent(event)
        self._position_currency_icon()

    def paintEvent(self,event):
        p=QPainter(self)
        p.fillRect(self.rect(),QColor('#29404a' if self.hovered else self.bg))
        p.end()
        super().paintEvent(event)

    def enterEvent(self, event):
        self.hovered=True;self.update()
        if self.details:
            QToolTip.showText(self.mapToGlobal(QPoint(self.width()+8,0)),self.details,self)
        super().enterEvent(event)

    def leaveEvent(self, event):
        self.hovered=False;self.update()
        QToolTip.hideText()
        super().leaveEvent(event)

    def event(self,event):
        if event.type()==QEvent.Type.ToolTip and self.details:
            QToolTip.showText(self.mapToGlobal(QPoint(self.width()+8,0)),self.details,self)
            return True
        return super().event(event)

def crest_tooltip(currency, maximum_override=None):
    if not currency:
        return '<b>Currency Progress</b><br><span style="color:#aaa">No saved data</span>'
    owned=currency.get('quantity',0) or 0
    earned=currency.get('totalEarned')
    if earned is None: earned=currency.get('quantityEarnedThisWeek')
    maximum=currency.get('maxWeeklyQuantity')
    try:
        if maximum is None or int(maximum) <= 0:
            maximum=currency.get('maxQuantity')
    except (TypeError,ValueError):
        maximum=currency.get('maxQuantity')
    maximum=maximum or 0
    if maximum_override is not None:
        maximum=maximum_override
    def number(x):
        try:return format(int(x),',')
        except (TypeError,ValueError):return 'Unknown'
    return ('<div style="font-size:12px"><b>Currency Progress</b><br>'
            '<span style="color:#ffdd2d">Owned:</span> '+number(owned)+'<br>'
            '<span style="color:#ffdd2d">Season Earned:</span> '+number(earned)+'<br>'
            '<span style="color:#ffdd2d">Season Maximum:</span> '+(number(maximum) if maximum else 'No limit')+'</div>')

# Offline approximations; the precise WoW rating color API is unavailable outside the client.
TRACK_ORDER={'adventurer':0,'veteran':1,'champion':2,'hero':3,'myth':4}
TRACK_COLORS={'adventurer':'#aeb7bd','veteran':'#40c46d','champion':'#4da9ff','hero':'#b65cf2','myth':'#ff982d'}
def minimum_equipment_track(c):
    tracks=[str(x.get('itemUpgradeTrack') or '').lower().strip() for x in arr(obj(c.get('data')).get('equipment')) if isinstance(x,dict)]
    valid=[t for t in tracks if t in TRACK_ORDER]
    return TRACK_COLORS[min(valid,key=lambda t:TRACK_ORDER[t])] if valid else None

def rating_color(value):
    try:v=float(value)
    except (TypeError,ValueError):return '#e8e8e8'
    if v<=0:return '#9d9d9d'
    if v<750:return '#ffffff'
    if v<1500:return '#1eff00'
    if v<2000:return '#0070dd'
    if v<2500:return '#a335ee'
    return '#ff8000'

def keystone_level_color(value):
    """Approximate WoW key-level quality colors, consistent with M+ palette."""
    try: level=int(value)
    except (TypeError,ValueError): return '#e8e8e8'
    if level >= 10: return '#ff8000'
    if level >= 7: return '#a335ee'
    if level >= 4: return '#0070dd'
    if level >= 2: return '#1eff00'
    return '#ffffff'

def currency_tooltip(name):
    """Static item descriptions displayed on the left currency-name column.
    No per-character balances or invented progress are included.
    """
    title='<span style="color:#a335ee;font-weight:700">'+escape(name)+'</span>'
    if name=='Nebulous Voidcore':
        body=('Transmutable into powerful equipment upon defeating powerful foes in '
              'Midnight raids or completing Mythic+ dungeons, Bountiful Delves and Prey Hunts on Nightmare difficulty.'
              '<br><br>Items may be received once per difficulty level until all potential items '
              'for your current specialization have been transmuted.')
    elif 'Mistcrest' in name:
        tier=name.replace(' Mistcrest','')
        body='Used to upgrade '+escape(tier)+' equipment in Midnight Season 2'
        if tier=='Adventurer':
            body+=' up to item levels 269-282.'
        else:
            body+='.'
        body+=('<br>When used for crafting, sets the item level of the resulting item '
               'based on Quality.')
        if tier=='Adventurer':body+=' (266-279)'
        body+=('<br><br>Earned from the following activities:<br>'
               '- Repeatable Outdoor Events<br>- Tier 4 Delves') if tier=='Adventurer' else ''
    else:
        body=''
    return '<div style="font-size:12px">'+title+'<br><span style="color:#ffdd2d">'+body+'</span></div>'

def dungeon_tooltip(name, dungeon):
    """Use only the run fields actually present in AlterEgo SavedVariables."""
    d = obj(dungeon)
    if not d or not d.get('level'):
        return ('<div style="font-size:12px"><b>'+escape(name)+'</b><br>'
                '<span style="color:#999">No recorded best run</span></div>')
    level = d.get('level')
    rating = d.get('rating', 0) or 0
    try: rating_text = str(round(float(rating)))
    except (TypeError,ValueError): rating_text = 'Unknown'
    html = ('<div style="font-size:12px"><b>'+escape(name)+'</b><br>'
            '<span style="color:#ffdd2d">Rating:</span> '
            '<span style="color:'+keystone_level_color(level)+'"><b>'+escape(rating_text)+'</b></span><br><br>'
            '<span style="color:#ffdd2d">Best Run</span><br>'
            'Level '+escape(str(level)))
    # Time is optional: many AlterEgo versions do not save it.
    time_value = next((d[k] for k in ('durationMS','durationMs','duration','completionTime','timeMS','time')
                       if d.get(k) is not None), None)
    if time_value is not None:
        try:
            seconds = int(float(time_value) / 1000) if float(time_value) > 3600 else int(float(time_value))
            if seconds >= 0: html += '<br>'+str(seconds//60)+':'+str(seconds%60).zfill(2)
        except (ValueError,TypeError): pass
    return html+'</div>'

def vault_raid_tooltip(c):
    """Raid vault tooltip from weekly activityEncounterInfo, not raid lockouts."""
    vault=obj(obj(c['data']).get('vault'))
    slots=sorted((x for x in arr(vault.get('slots')) if isinstance(x,dict) and x.get('type')==3),key=lambda x:x.get('index',0))[:3]
    html=['<div style="font-size:12px;color:#eeeeee"><b>Vault Progress</b><br>']
    def tier(level):
        try: level=int(level or 0)
        except (TypeError,ValueError): level=0
        if level>=16:return 'Mythic','#e49a28'
        if level==15:return 'Heroic','#b66bea'
        if level>0:return 'Normal','#48ca6a'
        return '—','#88939a'
    for slot in slots:
        threshold=int(slot.get('threshold') or 0)
        progress=int(slot.get('progress') or 0)
        unlocked=threshold>0 and progress>=threshold
        value,color=tier(slot.get('level') if unlocked else 0)
        html.append('<span style="color:#ffdd2d">'+str(threshold)+' bosses:</span> '
                    '<span style="color:'+color+'">'+escape(value)+'</span>'
                    +('' if unlocked else ' <span style="color:#9ca5aa">('+str(progress)+'/'+str(threshold)+')</span>')+'<br>')
    # uiOrder is the raid-wide position, independent of saved lockout difficulty.
    # index 1 is the first vault activity set; duplicate indices are not extra bosses.
    info=[x for x in arr(vault.get('activityEncounterInfo')) if isinstance(x,dict) and x.get('type')==3]
    groups={}
    for entry in info:
        idx=entry.get('index',1)
        groups.setdefault(idx,[]).append(entry)
    if groups:
        selected=groups.get(1) or groups[min(groups)]
        by_order={}
        for entry in selected:
            order=entry.get('uiOrder')
            if isinstance(order,int) and 0<=order<=9:
                if order not in by_order or int(entry.get('bestDifficulty') or 0)>int(by_order[order].get('bestDifficulty') or 0):
                    by_order[order]=entry
        if by_order:
            html.append('<div style="height:9px"></div>')
            for rn,bosses,start in zip(RAID_NAMES[:3],RAID_BOSSES[:3],[0,1,9]):
                html.append('<div style="height:7px"></div><span style="color:#ffdd2d">'+escape(rn)+'</span><br>')
                for i,boss in enumerate(bosses):
                    entry=by_order.get(start+i)
                    level=entry.get('bestDifficulty') if entry else 0
                    name,color=tier(level)
                    html.append('<span style="color:'+('#48d76a' if name!='—' else '#888888')+'">'+escape(boss)+'</span> '
                                '<span style="color:'+color+'">'+escape(name)+'</span><br>')
    html.append('</div>')
    return ''.join(html)

def vault_activity_tooltip(c, kind):
    """Great Vault dungeon/world progress, based on SavedVariables slots."""
    vault = obj(obj(c['data']).get('vault'))
    slots = sorted((x for x in arr(vault.get('slots'))
                    if isinstance(x, dict) and x.get('type') == kind),
                   key=lambda x: x.get('index', 0))[:3]
    label = 'dungeons' if kind == 1 else 'activities'
    html = ['<div style="font-size:12px;color:#eeeeee"><b>Vault Progress</b><br>']
    for i, slot in enumerate(slots):
        threshold = int(slot.get('threshold') or 0)
        progress = int(slot.get('progress') or 0)
        unlocked = threshold > 0 and progress >= threshold
        level = slot.get('level') if unlocked else None
        if unlocked and level:
            result = 'Tier ' + escape(str(level))
        elif unlocked:
            result = 'Unlocked'
        else:
            result = 'Locked (' + str(progress) + '/' + str(threshold) + ')'
        html.append('<span style="color:#ffdd2d">'+str(threshold)+' '+label+':</span> '
                    '<span style="color:'+('#eeeeee' if unlocked else '#999999')+'">'+result+'</span><br>')
    if not slots:
        html.append('<span style="color:#999999">No saved vault progress</span><br>')
    # Keep the advice accurate even when the addon has not saved a weekly run list.
    if slots:
        remaining = next((s for s in slots if int(s.get('progress') or 0) < int(s.get('threshold') or 0)), None)
        html.append('<br><span style="color:#ffdd2d">Next Step:</span><br>')
        if remaining:
            needed = int(remaining.get('threshold') or 0) - int(remaining.get('progress') or 0)
            if kind == 1:
                html.append('Complete '+str(needed)+' more qualifying dungeon'+('s' if needed != 1 else '')+' to unlock the next Great Vault reward.')
            else:
                html.append('Complete '+str(needed)+' more qualifying world activit'+('ies' if needed != 1 else 'y')+' to unlock the next Great Vault reward.')
        else:
            html.append('All three Great Vault reward slots are unlocked.')
    html.append('</div>')
    return ''.join(html)

class RaidHeaders(QWidget):
    def __init__(self,parent=None):
        super().__init__(parent)
        self.muted_theme=False
        self.setMinimumWidth(SKULL_WIDTH)
        self.setFixedHeight(18)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.row_bg='#123341'
        self.on_click=None
        self.setCursor(Qt.CursorShape.PointingHandCursor)
    def mousePressEvent(self,event):
        if event.button()==Qt.MouseButton.LeftButton and self.on_click:
            self.on_click()
        super().mousePressEvent(event)
    def paintEvent(self,event):
        p=QPainter(self)
        p.fillRect(self.rect(),QColor(self.row_bg))
        p.setPen(QColor('#aeb8c0' if self.muted_theme else '#ffdd2d'))
        f=p.font();f.setPointSize(8);f.setBold(True);p.setFont(f)
        for label,start,length in [('TG',0,1),('VA',1,8),('UoK',9,1),('MoQ',10,2)]:
            center=(skull_x(start,self.width())+skull_x(start+length-1,self.width()))/2+8
            p.drawText(QRect(round(center-14),0,28,self.height()),Qt.AlignmentFlag.AlignCenter,label)

# Icon images are downloaded only as needed and cached independently of the .pyw file.
# The Wowhead item tooltip supplies the icon *name*, not the SavedVariables numeric
# FileDataID; the corresponding icon image is then downloaded from Wowhead's CDN.
ICON_DIR = APP_DIR / 'icons' / 'items'

class EquipmentIconCache:
    """Asynchronous Qt-network icon loader, shared by all equipment windows."""
    def __init__(self):
        self.network = QNetworkAccessManager(QApplication.instance())
        self.pending = {}
        self.inflight = set()
        self.queue = []
        self.active = 0
        self.max_active = 4
        self.icon_names = {}
        self.ready = False
        try:
            ICON_DIR.mkdir(parents=True, exist_ok=True)
            manifest = ICON_DIR / 'manifest.json'
            if manifest.exists():
                loaded = json.loads(manifest.read_text(encoding='utf-8'))
                if isinstance(loaded, dict):
                    self.icon_names = {str(k):v for k,v in loaded.items() if isinstance(v,str) and re.fullmatch(r'[a-z0-9_]+',v)}
            self.ready = True
        except (OSError, ValueError):
            # The app remains usable if the icon directory is not writable.
            pass

    def request(self, item, label):
        link = str(item.get('itemLink') or '')
        match = re.search(r'\|Hitem:(\d+)', link)
        if not match:
            return
        item_id = match.group(1)
        cached = ICON_DIR / (item_id + '.jpg')
        if cached.is_file():
            pixmap = QPixmap(str(cached))
            if not pixmap.isNull():
                self._apply(label, pixmap)
                return
        if not self.ready:
            return
        self.pending.setdefault(item_id, []).append(weakref.ref(label))
        if item_id not in self.inflight:
            self.inflight.add(item_id)
            self.queue.append(item_id)
            self._pump()

    @staticmethod
    def _apply(label, pixmap):
        try:
            label.setText('')
            size = int(label.property('iconSize') or 39)
            label.setPixmap(pixmap.scaled(size,size,Qt.AspectRatioMode.KeepAspectRatio,Qt.TransformationMode.SmoothTransformation))
        except RuntimeError:
            pass

    def _pump(self):
        while self.queue and self.active < self.max_active:
            item_id = self.queue.pop(0)
            self.active += 1
            slug = self.icon_names.get(item_id)
            if slug:
                self._download_image(item_id,slug)
            else:
                url = QUrl('https://nether.wowhead.com/tooltip/item/'+item_id+'?dataEnv=live')
                request = QNetworkRequest(url)
                request.setRawHeader(b'User-Agent',b'AlterEgoDesktop/3.2')
                reply = self.network.get(request)
                reply.finished.connect(lambda r=reply, i=item_id: self._tooltip_done(i,r))

    def _tooltip_done(self, item_id, reply):
        try:
            data = bytes(reply.readAll())
            parsed = json.loads(data.decode('utf-8'))
            slug = parsed.get('icon') if isinstance(parsed,dict) else None
            if isinstance(slug,str) and re.fullmatch(r'[a-zA-Z0-9_]+',slug):
                slug=slug.lower()
                self.icon_names[item_id]=slug
                try:
                    (ICON_DIR/'manifest.json').write_text(json.dumps(self.icon_names,ensure_ascii=False),encoding='utf-8')
                except OSError:
                    pass
                self._download_image(item_id,slug)
                return
        except (ValueError, UnicodeError, RuntimeError):
            pass
        finally:
            reply.deleteLater()
        self._complete(item_id,None)

    def _download_image(self,item_id,slug):
        url = QUrl('https://wow.zamimg.com/images/wow/icons/large/'+slug+'.jpg')
        request = QNetworkRequest(url)
        reply = self.network.get(request)
        reply.finished.connect(lambda r=reply,i=item_id: self._image_done(i,r))

    def _image_done(self,item_id,reply):
        pixmap = QPixmap()
        try:
            data=bytes(reply.readAll())
            if reply.error()==QNetworkReply.NetworkError.NoError and pixmap.loadFromData(data) and not pixmap.isNull():
                try:
                    (ICON_DIR/(item_id+'.jpg')).write_bytes(data)
                except OSError:
                    pass
            else:
                pixmap=QPixmap()
        except (RuntimeError,OSError):
            pixmap=QPixmap()
        finally:
            reply.deleteLater()
        self._complete(item_id,pixmap if not pixmap.isNull() else None)

    def _complete(self,item_id,pixmap):
        for label_ref in self.pending.pop(item_id,[]):
            label=label_ref()
            if label is not None and pixmap is not None:
                self._apply(label,pixmap)
        self.inflight.discard(item_id)
        self.active=max(0,self.active-1)
        self._pump()

_icon_cache = None
def equipment_icons():
    global _icon_cache
    if _icon_cache is None:
        _icon_cache = EquipmentIconCache()
    return _icon_cache

class GemInfoCache:
    """Fetch gem descriptions asynchronously. Data may be absent; never invent stats."""
    def __init__(self):
        self.network=QNetworkAccessManager(QApplication.instance())
        self.waiters={}
        self.data={}
        self.names={}
        self.folder=APP_DIR/'icons'/'gem_info'
        try:self.folder.mkdir(parents=True,exist_ok=True)
        except OSError:pass

    def request(self,gem_id,callback):
        if gem_id in self.data:
            callback(self.data[gem_id]);return
        path=self.folder/(gem_id+'.txt')
        if path.exists():
            try:
                raw=path.read_text(encoding='utf-8')
                value=json.loads(raw)
                if isinstance(value,dict) and value.get('name') and value.get('stats'):
                    self.names[gem_id]=value['name']
                    self.data[gem_id]=value['stats'];callback(value['stats']);return
                # Old cache only stored stats, never a verified gem name.
                path.unlink(missing_ok=True)
            except (OSError, ValueError, TypeError):
                # Existing v3.8 cache is plain text, not JSON. Never let a
                # stale cache entry prevent the Equipment dialog from opening.
                try:path.unlink(missing_ok=True)
                except OSError:pass
        if gem_id in self.waiters:
            self.waiters[gem_id].append(callback);return
        self.waiters[gem_id]=[callback]
        request=QNetworkRequest(QUrl('https://nether.wowhead.com/tooltip/item/'+gem_id+'?dataEnv=live'))
        reply=self.network.get(request)
        reply.finished.connect(lambda r=reply,i=gem_id:self.done(i,r))

    def done(self,gem_id,reply):
        description='Gem '+gem_id+' (stats unavailable)'
        try:
            data=json.loads(bytes(reply.readAll()).decode('utf-8'))
            html=str(data.get('tooltip','')) if isinstance(data,dict) else ''
            if isinstance(data,dict) and data.get('name'):
                self.names[gem_id]=str(data['name'])
            # The tooltip is HTML generated by Wowhead. Prefer actual stat-bearing lines.
            html=re.sub(r'<br\s*/?>','\n',html,flags=re.I)
            html=re.sub(r'</(?:div|tr|li|span)>','\n',html,flags=re.I)
            plain=unescape(re.sub(r'<[^>]+>','',html))
            lines=[re.sub(r'\s+',' ',line).strip() for line in plain.splitlines()]
            lines=[line for line in lines if line]
            stats=[line for line in lines if re.search(r'(?:\+\s*\d+|Haste|Critical Strike|Mastery|Versatility|Agility|Strength|Intellect|Stamina)',line,re.I)]
            if lines and gem_id not in self.names:
                candidate=lines[0][:100]
                if not re.search(r'(?:\+\s*\d+|Haste|Critical Strike|Mastery|Versatility|Agility|Strength|Intellect|Stamina)',candidate,re.I):
                    self.names[gem_id]=candidate
            if stats:description='; '.join(dict.fromkeys(stats[:3]))[:260]
            elif lines:description='; '.join(lines[:3])[:260]
            if description!='Gem '+gem_id+' (stats unavailable)':
                try:(self.folder/(gem_id+'.txt')).write_text(json.dumps({'name':self.names.get(gem_id,''),'stats':description},ensure_ascii=False),encoding='utf-8')
                except OSError:pass
        except (ValueError,UnicodeError,RuntimeError):pass
        finally:reply.deleteLater()
        self.data[gem_id]=description
        for callback in self.waiters.pop(gem_id,[]):callback(description)

_gem_info_cache=None
def gem_info_cache():
    global _gem_info_cache
    if _gem_info_cache is None:_gem_info_cache=GemInfoCache()
    return _gem_info_cache

class EnchantInfoCache:
    """Resolve permanent enchant IDs from Wowhead's enchanted-item tooltip.

    The enchant field in a WoW item link is NOT a spell or item ID.
    Wowhead supports the enchant ID as an `ench` item-tooltip modifier.
    """
    def __init__(self):
        self.network=QNetworkAccessManager(QApplication.instance())
        self.folder=APP_DIR/'icons'/'enchant_info'
        try:self.folder.mkdir(parents=True,exist_ok=True)
        except OSError:pass
        self.data={};self.waiters={}

    def request(self,enchant_id,item_id,callback):
        key=str(enchant_id)
        if key in self.data:
            callback(self.data[key]);return
        path=self.folder/(key+'.txt')
        if path.exists():
            try:
                value=path.read_text(encoding='utf-8').strip()
                if value and re.match(r'^(?:Enchanted|Enchant(?:ment)?):\s*',value,re.I):
                    self.data[key]=re.sub(r'^(?:Enchanted|Enchant(?:ment)?):\s*','',value,flags=re.I);callback(self.data[key]);return
            except OSError:pass
        if key in self.waiters:
            self.waiters[key].append(callback);return
        self.waiters[key]=[callback]
        # Request a real item tooltip with this enchant applied. The returned
        # tooltip includes the enchant name when Wowhead recognizes the ID.
        url='https://nether.wowhead.com/tooltip/item/'+str(item_id)+'?dataEnv=live&ench='+key
        req=QNetworkRequest(QUrl(url))
        reply=self.network.get(req)
        reply.finished.connect(lambda r=reply,k=key:self._done(k,r))

    def _done(self,key,reply):
        result=None
        try:
            if reply.error()==QNetworkReply.NetworkError.NoError:
                payload=json.loads(bytes(reply.readAll()).decode('utf-8'))
                html=str(payload.get('tooltip','')) if isinstance(payload,dict) else ''
                # Convert HTML into individual lines, retaining the enchant
                # description but not the equipped item's name or stat lines.
                html=re.sub(r'<br\s*/?>','\n',html,flags=re.I)
                html=re.sub(r'</(?:div|tr|li|p)>','\n',html,flags=re.I)
                plain=unescape(re.sub(r'<[^>]+>','',html))
                lines=[re.sub(r'\s+',' ',x).strip() for x in plain.splitlines()]
                for line in lines:
                    m=re.search(r'^(?:Enchanted|Enchant(?:ment)?):\s*(.+)$',line,re.I)
                    if m:
                        candidate=m.group(1).strip()
                        if candidate and len(candidate)<180 and not re.match(r'^\d+$',candidate):
                            result=candidate;break
                # Never infer an enchant name from an arbitrary green item stat.
                # Without an explicit enchant marker the result is unknown.
        except (ValueError,UnicodeError,RuntimeError):pass
        finally:reply.deleteLater()
        if result:
            self.data[key]=result
            try:(self.folder/(key+'.txt')).write_text('Enchanted: '+result,encoding='utf-8')
            except OSError:pass
        for callback in self.waiters.pop(key,[]):callback(result)

_enchant_info_cache=None
def enchant_info_cache():
    global _enchant_info_cache
    if _enchant_info_cache is None:_enchant_info_cache=EnchantInfoCache()
    return _enchant_info_cache

class GearHover(QLabel):
    """Non-activating tooltip without Qt's short automatic timeout."""
    def __init__(self,parent):
        super().__init__(None,Qt.WindowType.ToolTip|Qt.WindowType.FramelessWindowHint)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating,True)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents,True)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating,True)
        self.setTextFormat(Qt.TextFormat.RichText)
        self.setWordWrap(True)
        self.setMaximumWidth(350)
        self.setStyleSheet('background:#17171c;color:#eeeeee;border:1px solid #555;padding:8px;font-size:12px;')

    def display(self,html,point):
        self.setText(html);self.adjustSize();self.move(point);self.show()

class GearTile(QFrame):
    """Compact equipment slot; data is read-only from AlterEgo."""
    QUALITY={0:'#9d9d9d',1:'#eeeeee',2:'#1eff00',3:'#0070dd',4:'#a335ee',5:'#ff8000',6:'#e6cc80'}
    TRACK={'myth':'#ff982d','hero':'#b65cf2','champion':'#4da9ff','veteran':'#40c46d','adventurer':'#aeb7bd','crafted':'#53d994'}
    def __init__(self, slot, item, parent=None):
        super().__init__(parent)
        self.item=item
        self.slot=slot
        self.setObjectName('gearTile');self.setFixedHeight(53)
        self.setStyleSheet('QFrame#gearTile{background:#1d2226;border:1px solid #30383e;border-radius:3px;} QFrame#gearTile:hover{background:#30424c;border:1px solid #6b8899;} QLabel{background:transparent;border:none;}')
        row=QHBoxLayout(self);row.setContentsMargins(5,3,5,3);row.setSpacing(9)
        track=str(item.get('itemUpgradeTrack') or '').strip() if item else ''
        quality=int(item.get('itemQuality') or 1) if item else 1
        color=self.TRACK.get(track.lower(),self.QUALITY.get(quality,'#eeeeee'))
        icon=QLabel('◇' if item else '—');icon.setFixedSize(43,43);icon.setAlignment(Qt.AlignmentFlag.AlignCenter)
        icon.setStyleSheet('background:#10171b;border:2px solid '+(self.QUALITY.get(quality,'#eeeeee') if item else '#4a5156')+';border-radius:2px;color:'+color+';font-size:22px;')
        row.addWidget(icon)
        if item: equipment_icons().request(item,icon)
        self._hover=GearHover(self)
        self._gem_badges=[]
        self._gem_descriptions={}
        self._active_hover_key=None
        self._icon=icon
        self._enchant_id=None
        self._star=None
        self._missing_socket_label=None
        if item:
            item_link=str(item.get('itemLink') or '')
            match=re.search(r'\|Hitem:([^|]+)',item_link)
            parts=match.group(1).split(':') if match else []
            self._gem_ids=[raw for raw in parts[2:6] if raw.isdigit() and int(raw)>0]
            for index,gem_id in enumerate(self._gem_ids[:2]):
                badge=QLabel(icon)
                badge.setProperty('iconSize',16)
                badge.setFixedSize(19,19)
                badge.setAlignment(Qt.AlignmentFlag.AlignCenter)
                badge.setStyleSheet('background:#12171b;border:1px solid #d8d8d8;border-radius:2px;color:white;font-size:9px;')
                badge.move(43-19-index*15,43-19)
                badge.setText('◆')
                badge.setToolTip('')
                badge.setProperty('gemId',gem_id)
                badge.setMouseTracking(True)
                badge.installEventFilter(self)
                equipment_icons().request({'itemLink':'|Hitem:'+gem_id+':|h'},badge)
                gem_info_cache().request(gem_id,lambda desc,g=gem_id:self._gem_description_ready(g,desc))
                badge.raise_()
                self._gem_badges.append(badge)
            if self._gem_ids:icon.setProperty('gemIds',', '.join(self._gem_ids))
        else:self._gem_ids=[]
        self.setMouseTracking(True)
        icon.setMouseTracking(True)
        icon.installEventFilter(self)
        if item:
            self.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
            self.customContextMenuRequested.connect(self._show_item_context_menu)
            icon.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
            icon.customContextMenuRequested.connect(self._show_item_context_menu)
        details_row=QHBoxLayout();details_row.setContentsMargins(0,0,0,0);details_row.setSpacing(8)
        details=QVBoxLayout();details.setContentsMargins(0,0,0,0);details.setSpacing(1)
        title=QLabel(slot);title.setStyleSheet('color:#c1cbd1;font-size:13px;');details.addWidget(title)
        if item:
            level=QLabel(str(item.get('itemLevel','—')))
            level.setStyleSheet('color:'+color+';font-size:15px;font-weight:700;')
            details.addWidget(level)
            link=str(item.get('itemLink') or '')
            m=re.search(r'\|Hitem:([^|]+)',link)
            fields=m.group(1).split(':') if m else []
            enchant=fields[1] if len(fields)>1 else ''
            enchanted=bool(enchant and enchant!='0')
            self._enchant_id=enchant if enchanted else None
            tooltip='<b><span style="color:'+self.QUALITY.get(quality,'#eeeeee')+';">'+escape(str(item.get('itemName') or slot))+'</span></b><br>Item Level: '+escape(str(item.get('itemLevel','—')))
            if track:tooltip+='<br>Upgrade Track: '+escape(track)
            gem_list=icon.property('gemIds')
            if gem_list:tooltip+='<br>Socketed Gems: '+escape(gem_list)
            # Only flag slots which commonly accept enchants; other slots remain unmarked.
            enchantable=slot in ('Head','Shoulders','Chest','Legs','Feet','Finger','Main Hand')
            if enchanted and enchantable:
                tooltip+='<br><span style="color:#f4c452">Enchanted</span>'
            elif enchantable:
                tooltip+='<br><span style="color:#ff6262">Not Enchanted</span>'
            self.setProperty('tooltipHtml',tooltip)
            icon.setProperty('tooltipHtml',tooltip)
        else:
            empty=QLabel('Empty');empty.setStyleSheet('color:#77848d;font-size:12px;');details.addWidget(empty)
        # Reserve identical horizontal regions for every slot, regardless of label length.
        # This keeps Upgrade Track centered in each column and in both weapon tiles.
        detail_holder=QWidget()
        detail_holder.setStyleSheet('background:transparent;border:none;')
        detail_holder.setFixedWidth(90)
        detail_holder.setLayout(details)
        details_row.addWidget(detail_holder)
        if item:
            label=track
            # Crafted items in saved WoW item links have crafted source context 13.
            if not label and ('crafted' in str(item.get('itemUpgradeTrack','')).lower() or '::13:' in str(item.get('itemLink') or '')):
                label='Crafted'
            current=item.get('itemUpgradeLevel')
            maximum=item.get('itemUpgradeMax')
            try:
                current=int(current)
                maximum=int(maximum)
                if label and label.lower()!='crafted' and maximum>0:
                    label=f'{label} {current}/{maximum}'
            except (TypeError, ValueError):
                pass
            # Upgrade text occupies the same centered region on every slot.
            missing=QLabel('Missing\nSocket')
            missing.setFixedSize(58,34)
            missing.setAlignment(Qt.AlignmentFlag.AlignCenter)
            missing.setStyleSheet('color:#ff6262;font-size:10px;font-weight:600;background:transparent;border:none;')
            missing.setVisible(False)
            missing.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents,True)
            self._missing_socket_label=missing
            upgrade=QLabel(label)
            self._upgrade_label=upgrade
            upgrade.setFixedWidth(82)
            details_row.addStretch(1)
            upgrade.setAlignment(Qt.AlignmentFlag.AlignCenter)
            upgrade.setStyleSheet('color:'+self.TRACK.get(track.lower(),'#53d994' if label=='Crafted' else '#9da7b0')+';font-size:12px;font-weight:700;')
            upgrade.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents,True)
            details_row.addWidget(upgrade)
            details_row.addStretch(1)
            # Floating indicator does not participate in layout sizing.
            missing.setParent(self)
            missing.raise_()
            # Only enchantable equipment gets a marker; no placeholder rectangles.
            if enchantable:
                star=QLabel('✦')
                star.setFixedSize(32,43)
                star.setAlignment(Qt.AlignmentFlag.AlignCenter)
                star.setStyleSheet('color:'+('#f4c452' if enchanted else '#ef5555')+';font-size:27px;')
                self._star=star
                star.setMouseTracking(True)
                star.installEventFilter(self)
                details_row.addWidget(star)
            else:
                # Invisible layout spacer only; never a visible dark rectangle.
                details_row.addSpacing(32)
        else:
            details_row.addStretch(1)
            details_row.addSpacing(32)
        row.addLayout(details_row,1)
    def _gem_description_ready(self,gem_id,description):
        self._gem_descriptions[gem_id]=description
        # Only refresh when the cursor is STILL on this gem.
        if self._active_hover_key==('gem',gem_id):
            self._sync_hover(force=True)

    def _position_missing_socket(self):
        if self._missing_socket_label is None or not hasattr(self,'_upgrade_label'):return
        # Anchor to the track label, never move the track itself.
        target=self._upgrade_label
        center=target.mapTo(self,QPoint(0,target.height()//2))
        # Keep the marker in the gap between the left details and the track.
        # This widget is not in the layout, so it never shifts the track.
        left_edge=self._icon.geometry().right()+9+90
        right_edge=center.x()-4
        x=left_edge+max(0,(right_edge-left_edge-self._missing_socket_label.width())//2)
        self._missing_socket_label.move(x,max(0,center.y()-self._missing_socket_label.height()//2))
        self._missing_socket_label.raise_()

    def resizeEvent(self,event):
        super().resizeEvent(event)
        QTimer.singleShot(0,self._position_missing_socket)

    def _sync_hover(self,force=False):
        # Determine the actual widget under the global cursor. Parent enter/move
        # events are also generated while crossing a gem child; they must never
        # overwrite the gem's tooltip with the parent item tooltip.
        pos=QCursor.pos()
        local=self.mapFromGlobal(pos)
        if not self.rect().contains(local):
            key=None
        else:
            key=('item',None)
            if self._star is not None and self._star.rect().contains(self._star.mapFromGlobal(pos)):
                key=('enchant',None)
            for badge in self._gem_badges:
                if badge.isVisible() and badge.rect().contains(badge.mapFromGlobal(pos)):
                    key=('empty',None) if badge.property('emptySocket') else ('gem',str(badge.property('gemId')))
                    break
        if key==self._active_hover_key and not force:return
        self._active_hover_key=key
        if key is None:
            self._hover.hide()
            return
        kind,gem_id=key
        if kind=='empty':
            html='<b><span style="color:#ff6262">Empty Socket</span></b><br>Test preview — no gem inserted'
        elif kind=='enchant':
            if self._enchant_id:
                html='<b><span style="color:#f4c452">Enchanted</span></b>'
            else:
                html='<b><span style="color:#ff6262">Not Enchanted</span></b>'
        elif kind=='gem':
            desc=self._gem_descriptions.get(gem_id,'Loading gem stats…')
            gem_name=gem_info_cache().names.get(gem_id)
            html='<b>'+escape(gem_name or 'Gem '+gem_id)+'</b><br>'+escape(desc)
        else:
            html=self.property('tooltipHtml')
        if not html:
            self._hover.hide()
            return
        if kind=='enchant' and self._star is not None:
            point=self._star.mapToGlobal(QPoint(self._star.width()+8,0))
        elif kind in ('gem','empty'):
            badge=next((b for b in self._gem_badges if
                        (b.property('emptySocket') if kind=='empty' else str(b.property('gemId') or '')==gem_id)),None)
            point=badge.mapToGlobal(QPoint(badge.width()+8,0)) if badge else self.mapToGlobal(QPoint(self.width()+8,0))
        else:
            point=self.mapToGlobal(QPoint(self.width()+8,0))
        self._hover.display(html,point)

    def eventFilter(self,obj,event):
        if event.type() in (QEvent.Type.Enter,QEvent.Type.MouseMove,QEvent.Type.Leave):
            # Defer until Qt finishes dispatching child enter/leave events.
            QTimer.singleShot(0,self._sync_hover)
        elif event.type()==QEvent.Type.ToolTip:
            # Prevent QLabel's native tooltip from competing with GearHover.
            return True
        return super().eventFilter(obj,event)

    def enterEvent(self,event):
        super().enterEvent(event)
        QTimer.singleShot(0,self._sync_hover)

    def mouseMoveEvent(self,event):
        super().mouseMoveEvent(event)
        QTimer.singleShot(0,self._sync_hover)

    def leaveEvent(self,event):
        super().leaveEvent(event)
        QTimer.singleShot(0,self._sync_hover)

    def _show_item_context_menu(self, _pos):
        if not self.item:
            return
        menu=QMenu(self)
        action=menu.addAction('Проверить наличие предмета на других персонажах')
        action.triggered.connect(self._check_item_on_other_characters)
        menu.exec(QCursor.pos())

    def _check_item_on_other_characters(self):
        dialog=self.window()
        if isinstance(dialog,GearDialog):
            dialog.check_item_on_other_characters(self.item)

class GearDialog(QDialog):
    LABELS={'HEADSLOT':'Head','NECKSLOT':'Neck','SHOULDERSLOT':'Shoulders','BACKSLOT':'Back','CHESTSLOT':'Chest','WRISTSLOT':'Wrist','SHIRTSLOT':'Shirt','TABARDSLOT':'Tabard','HANDSSLOT':'Hands','WAISTSLOT':'Waist','LEGSSLOT':'Legs','FEETSLOT':'Feet','MAINHANDSLOT':'Main Hand','SECONDARYHANDSLOT':'Off Hand','FINGERSLOT':'Finger','FINGER0SLOT':'Finger','FINGER1SLOT':'Finger','TRINKET0SLOT':'Trinket','TRINKET1SLOT':'Trinket'}
    def __init__(self,c,parent=None):
        super().__init__(parent)
        info=obj(c.get('info'));name=str(info.get('name','Character'))
        self.character=c
        self.character_key=c.get('key')
        self.parent_window=parent
        self.setWindowTitle('Equipment — '+name);self.resize(760,735);self.setMinimumSize(760,650)
        outer=QVBoxLayout(self);outer.setContentsMargins(12,10,12,10);outer.setSpacing(7)
        top=QHBoxLayout();head=QLabel(name+' <span style="color:#87949d">- '+escape(str(info.get('realm','')))+'</span>');head.setStyleSheet('font-size:16px;font-weight:700');top.addWidget(head);top.addStretch()
        close=QPushButton('✕');close.setFixedWidth(32);close.clicked.connect(self.close);top.addWidget(close);outer.addLayout(top)
        ilvl=info.get('ilvl') or info.get('itemLevel')
        if isinstance(ilvl,dict):ilvl=ilvl.get('equipped',ilvl.get('level'))
        try:level=f'{float(ilvl):.1f}'
        except (TypeError,ValueError):level='—'
        summary=QLabel('Item Level: '+level);summary.setAlignment(Qt.AlignmentFlag.AlignCenter);summary.setStyleSheet('color:#c77df0;font-size:17px;font-weight:700');outer.addWidget(summary)
        scroll=QScrollArea();scroll.setWidgetResizable(True);outer.addWidget(scroll)
        content=QWidget();scroll.setWidget(content);grid=QGridLayout(content)
        grid.setContentsMargins(1,1,1,1);grid.setHorizontalSpacing(35);grid.setVerticalSpacing(3)
        gear=arr(obj(c.get('data')).get('equipment'))
        slot_map={};extra=[]
        for item in gear:
            if not isinstance(item,dict):continue
            slot=str(item.get('itemSlotName') or '')
            if slot in ('FINGERSLOT','FINGER0SLOT','FINGER1SLOT'):
                slot='FINGER0SLOT' if 'FINGER0SLOT' not in slot_map else 'FINGER1SLOT'
            elif slot in ('TRINKETSLOT','TRINKET0SLOT','TRINKET1SLOT'):
                slot='TRINKET0SLOT' if 'TRINKET0SLOT' not in slot_map else 'TRINKET1SLOT'
            elif slot in slot_map:extra.append(item);continue
            slot_map[slot]=item
        left=['HEADSLOT','NECKSLOT','SHOULDERSLOT','BACKSLOT','CHESTSLOT','SHIRTSLOT','TABARDSLOT','WRISTSLOT']
        right=['HANDSSLOT','WAISTSLOT','LEGSSLOT','FEETSLOT','FINGER0SLOT','FINGER1SLOT','TRINKET0SLOT','TRINKET1SLOT']
        for col,slots in enumerate((left,right)):
            for i,slot in enumerate(slots):
                grid.addWidget(GearTile(self.LABELS.get(slot,slot),slot_map.get(slot),self),i,col)
        # Weapon slots belong together at the bottom center, as in WoW's character pane.
        weapons=QWidget()
        weapon_row=QHBoxLayout(weapons)
        weapon_row.setContentsMargins(0,7,0,0)
        weapon_row.setSpacing(16)
        weapon_row.addStretch(1)
        for weapon_slot in ('MAINHANDSLOT','SECONDARYHANDSLOT'):
            tile=GearTile(self.LABELS[weapon_slot],slot_map.get(weapon_slot),self)
            tile.setFixedWidth(265)
            weapon_row.addWidget(tile)
        weapon_row.addStretch(1)
        grid.addWidget(weapons,8,0,1,2)
        for i,item in enumerate(extra):
            grid.addWidget(GearTile(str(item.get('itemSlotName','Other')),item,self),9+i,0,1,2)
        grid.setColumnStretch(0,1);grid.setColumnStretch(1,1)
    @staticmethod
    def _normalize_item_name(item):
        return re.sub(r'\s+', ' ', str(obj(item).get('itemName') or '').strip()).casefold()

    @staticmethod
    def _upgrade_state(item):
        item=obj(item)
        track=str(item.get('itemUpgradeTrack') or '').strip()
        key=track.casefold()
        if key not in TRACK_ORDER:
            return None
        try:
            level=int(item.get('itemUpgradeLevel'))
        except (TypeError,ValueError):
            return None
        try:
            maximum=int(item.get('itemUpgradeMax'))
        except (TypeError,ValueError):
            maximum=0
        return TRACK_ORDER[key], level, maximum, track

    @classmethod
    def _item_is_lower(cls, candidate, target):
        c=cls._upgrade_state(candidate)
        t=cls._upgrade_state(target)
        if c is None or t is None:
            return False
        c_track,c_level,_,_=c
        t_track,t_level,_,_=t
        return c_track < t_track or (c_track == t_track and c_level < t_level)

    @staticmethod
    def _item_upgrade_text(item):
        item=obj(item)
        track=str(item.get('itemUpgradeTrack') or '').strip()
        try:
            level=int(item.get('itemUpgradeLevel'))
            maximum=int(item.get('itemUpgradeMax'))
        except (TypeError,ValueError):
            level=maximum=None
        if track:
            if level is not None and maximum is not None and maximum>0:
                return f'{track} {level}/{maximum}'
            return track
        return 'Upgrade Track неизвестен'

    def check_item_on_other_characters(self, target_item):
        target_name=str(obj(target_item).get('itemName') or '').strip()
        normalized_name=self._normalize_item_name(target_item)
        if not normalized_name:
            QMessageBox.information(self,'Проверка предмета','У выбранного предмета нет сохранённого названия.')
            return

        chars=self.parent_window.filtered() if hasattr(self,'parent_window') else []
        current_key=getattr(self,'character_key',None)
        if not chars and self.parent() is not None:
            try:
                chars=self.parent().filtered()
            except Exception:
                chars=[]
        findings=[]
        for c in chars:
            if c.get('key')==current_key:
                continue
            equipment=arr(obj(c.get('data')).get('equipment'))
            matching=[x for x in equipment
                      if isinstance(x,dict) and self._normalize_item_name(x)==normalized_name]
            if not matching:
                findings.append((c,None))
                continue
            lower=next((x for x in matching if self._item_is_lower(x,target_item)),None)
            if lower is not None:
                findings.append((c,lower))

        dialog=QDialog(self)
        dialog.setWindowTitle('Check Item on Other Characters')
        dialog.resize(560,470)
        layout=QVBoxLayout(dialog)
        heading=QLabel('Check Item on Other Characters')
        heading.setStyleSheet('font-size:18px;font-weight:700;color:#ffdd2d;')
        layout.addWidget(heading)
        target_text=self._item_upgrade_text(target_item)
        target_label=QLabel('<b>'+escape(target_name)+'</b> — '+escape(target_text))
        target_label.setStyleSheet('color:#d6dde1;font-size:13px;')
        layout.addWidget(target_label)
        report=QScrollArea()
        report.setWidgetResizable(True)
        content=QWidget()
        stack=QVBoxLayout(content)
        if not chars:
            stack.addWidget(QLabel('No characters match the current filters.'))
        elif not findings:
            ok=QLabel('No characters have a lower version or are missing this item.')
            ok.setStyleSheet('font-size:15px;color:#5fe09b;font-weight:700;')
            stack.addWidget(ok)
        else:
            for c,item in findings:
                info=obj(c.get('info'))
                name=str(info.get('name') or 'Unknown')
                realm=str(info.get('realm') or '')
                num=self.parent_window.settings.get('numbers',{}).get(c.get('key'))
                prefix=str(num)+'. ' if str(num).isdigit() else ''
                color=character_class_color(info)
                account_name=str(self.parent_window.alias(c.get('account')))
                name_label=QLabel(
                    '<span style="color:'+color+';font-weight:700">'+escape(prefix+name)+'</span>'
                    '<span style="color:#aeb6ba"> - '+escape(realm)+' ('+escape(account_name)+')</span>'
                )
                name_label.setTextFormat(Qt.TextFormat.RichText)
                name_label.setStyleSheet('font-size:13px;')
                if item is None:
                    state='Нет предмета'
                    state_color='#ff6262'
                else:
                    state=self._item_upgrade_text(item)
                    state_color='#ffdd2d'
                state_label=QLabel(state)
                state_label.setStyleSheet('color:'+state_color+';font-size:12px;font-weight:600;')
                row_widget=QWidget()
                row_layout=QVBoxLayout(row_widget)
                row_layout.setContentsMargins(7,4,7,4)
                row_layout.setSpacing(1)
                row_layout.addWidget(name_label)
                row_layout.addWidget(state_label)
                row_widget.setStyleSheet('background:#1d2226;border:1px solid #30383e;border-radius:3px;')
                stack.addWidget(row_widget)
        stack.addStretch(1)
        report.setWidget(content)
        layout.addWidget(report)
        dialog.exec()


WOW_CLASS_COLORS={
    1:'#C69B6D',  # Warrior
    2:'#F48CBA',  # Paladin
    3:'#AAD372',  # Hunter
    4:'#FFF468',  # Rogue
    5:'#FFFFFF',  # Priest
    6:'#C41E3A',  # Death Knight
    7:'#0070DD',  # Shaman
    8:'#3FC7EB',  # Mage
    9:'#8788EE',  # Warlock
    10:'#00FF98', # Monk
    11:'#FF7C0A', # Druid
    12:'#A330C9', # Demon Hunter
    13:'#33937F', # Evoker
}
WOW_CLASS_NAMES={
    'WARRIOR':1,'PALADIN':2,'HUNTER':3,'ROGUE':4,'PRIEST':5,
    'DEATHKNIGHT':6,'SHAMAN':7,'MAGE':8,'WARLOCK':9,
    'MONK':10,'DRUID':11,'DEMONHUNTER':12,'EVOKER':13,
    # Localized WoW class names in case the addon saves a display name.
    'ВОИН':1,'ПАЛАДИН':2,'ОХОТНИК':3,'РАЗБОЙНИК':4,
    'ЖРЕЦ':5,'РЫЦАРЬСМЕРТИ':6,'ШАМАН':7,'МАГ':8,
    'ЧЕРНОКНИЖНИК':9,'МОНАХ':10,'ДРУИД':11,
    'ОХОТНИКНАДЕМОНОВ':12,'ПРОБУДИТЕЛЬ':13,
}

def character_class_color(info):
    """Resolve class per character, accepting numeric and token-based addon formats."""
    info=obj(info)
    class_value=info.get('class')
    class_info=obj(class_value)
    values=[
        class_info.get('id'),class_info.get('classID'),class_info.get('classId'),
        class_info.get('file'),class_info.get('filename'),
        class_info.get('name'),class_info.get('token'),
        info.get('classID'),info.get('classId'),info.get('classFile'),
        info.get('className'),info.get('classToken'),class_value,
    ]
    for value in values:
        if isinstance(value,bool):
            continue
        if isinstance(value,(int,float)) or (isinstance(value,str) and value.strip().isdigit()):
            try:
                class_id=int(value)
                if class_id in WOW_CLASS_COLORS:
                    return WOW_CLASS_COLORS[class_id]
            except (ValueError,TypeError,OverflowError):
                pass
        if isinstance(value,str):
            token=''.join(ch for ch in value.upper() if ch.isalpha())
            class_id=WOW_CLASS_NAMES.get(token)
            if class_id:
                return WOW_CLASS_COLORS[class_id]
    return '#aeb6ba'  # Unknown class must never default to hunter green.

class ManagerCheckBox(QCheckBox):
    def sizeHint(self):
        # Custom paint uses a 29px text inset, wider than Qt's native checkbox
        # indicator. Reserve that width so the last letter is not clipped.
        hint=super().sizeHint()
        hint.setWidth(max(hint.width(), 29+self.fontMetrics().horizontalAdvance(self.text())+12))
        return hint

    """High-contrast checkbox with a visible square and checkmark on dark teal."""
    def paintEvent(self,event):
        painter=QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        box=QRect(2,(self.height()-17)//2,17,17)
        # This checkbox is custom-painted: QSS alone cannot recolor it.
        # Read the active theme so Manage Characters matches the main window.
        manager=self.window()
        settings=getattr(manager,'settings',None)
        if settings is None: settings=getattr(getattr(manager,'window',None),'settings',{})
        dark=bool(settings.get('dark_mode',False))
        accent='#aeb8c0' if dark else '#ffdd2d'
        painter.setPen(QPen(QColor(accent),2))
        painter.setBrush(QBrush(QColor(accent) if self.isChecked() else QColor('#292b2e')))
        painter.drawRoundedRect(box,3,3)
        if self.isChecked():
            painter.setPen(QPen(QColor('#17191b'),2.3))
            painter.drawLine(box.left()+4,box.top()+9,box.left()+7,box.top()+12)
            painter.drawLine(box.left()+7,box.top()+12,box.left()+13,box.top()+5)
        painter.setPen(QColor('#ececec') if self.isEnabled() else QColor('#8898a2'))
        painter.drawText(QRect(29,0,max(0,self.width()-29),self.height()),
                         Qt.AlignmentFlag.AlignLeft|Qt.AlignmentFlag.AlignVCenter,self.text())
        painter.end()

class NoWheelComboBox(QComboBox):
    """A selector that changes only by explicit mouse selection, not wheel hover."""
    def wheelEvent(self, event):
        event.ignore()


class Manager(QDialog):
    def __init__(self,window):
        super().__init__(window);self.window=window;self.setWindowTitle('Manage Characters & Accounts');self.resize(520,630)
        self.setMinimumWidth(490)
        self.setObjectName('characterManager')
        self.setStyleSheet('''
            QDialog#characterManager, QDialog#characterManager QWidget {background:#17191b;color:#ececec;font:13px "Segoe UI";}
            QDialog#characterManager QScrollArea {background:#17191b;border:0;}
            QDialog#characterManager QCheckBox {spacing:9px;background:transparent;}
            QDialog#characterManager QComboBox {background:#292b2e;color:#ececec;border:1px solid #ffdd2d;border-radius:7px;padding:4px 8px;min-height:24px;}
            QDialog#characterManager QComboBox:focus {border-color:#ffe66d;}
            QDialog#characterManager QComboBox::drop-down {subcontrol-origin:padding;subcontrol-position:top right;width:22px;border-left:1px solid #ffdd2d;background:#292b2e;border-top-right-radius:6px;border-bottom-right-radius:6px;}
            QDialog#characterManager QComboBox QAbstractItemView {background:#242629;color:#ececec;border:1px solid #ffdd2d;outline:0;selection-background-color:#55503a;selection-color:#ffdd2d;padding:2px;}
            QDialog#characterManager QPushButton, QDialog#characterManager QDialogButtonBox QPushButton {background:#292b2e;color:#ececec;border:1px solid #ffdd2d;border-radius:7px;padding:6px 10px;font-weight:600;}
            QDialog#characterManager QPushButton:hover, QDialog#characterManager QDialogButtonBox QPushButton:hover {background:#454442;border-color:#ffe66d;}
            QDialog#characterManager QPushButton:pressed {background:#55503a;}
            QDialog#characterManager QToolButton {background:#292b2e;border:1px solid #ffdd2d;border-radius:6px;color:#ececec;}
            QDialog#characterManager QToolButton:hover {background:#454442;border-color:#ffe66d;}
            QDialog#characterManager QScrollBar:vertical {background:#242629;width:12px;margin:0;border:1px solid #3c3e40;border-radius:6px;}
            QDialog#characterManager QScrollBar::handle:vertical {background:#80712e;border-radius:5px;min-height:35px;margin:2px;}
            QDialog#characterManager QScrollBar::handle:vertical:hover {background:#ffdd2d;}
            QDialog#characterManager QScrollBar::add-line:vertical, QDialog#characterManager QScrollBar::sub-line:vertical {height:0;width:0;border:0;background:none;}
            QDialog#characterManager QScrollBar::add-page:vertical, QDialog#characterManager QScrollBar::sub-page:vertical {background:transparent;}
        ''')
        lay=QVBoxLayout(self)
        # Always-visible setup guidance, using the same 13px Segoe UI size
        # as the main window's theme toggle. No help button or tooltip.
        guidance=QLabel(
            'Здесь надо пронумеровать персонажей, удалить ненужных, а так же '
            'переименовать аккаунты в более комфортные, например @email '
            'который вы используете или #btag аккаунта')
        guidance.setObjectName('manageGuidance')
        guidance.setWordWrap(True)
        guidance.setAlignment(Qt.AlignmentFlag.AlignLeft|Qt.AlignmentFlag.AlignVCenter)
        guidance.setStyleSheet('QLabel#manageGuidance{color:#ffdd2d;font-size:13px;font-weight:500;'
                               'background:transparent;padding:3px 2px 8px 2px;}')
        lay.addWidget(guidance)
        scroll=QScrollArea();scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        lay.addWidget(scroll)
        body=QWidget();scroll.setWidget(body);self.inner=QVBoxLayout(body);self.inner.setContentsMargins(8,6,8,6);self.boxes={};self.order_boxes={}
        self.deleted=set(window.settings.get('deleted',[]))
        reload_button=QPushButton('↻  Reload all characters')
        reload_button.setToolTip('Restore locally removed characters and reread AlterEgo.lua files')
        reload_button.clicked.connect(self.reload_all)
        lay.insertWidget(1,reload_button)
        groups={}
        for c in window.characters:
            if c['key'] not in self.deleted:groups.setdefault(c['file'],[]).append(c)
        for path,chars in sorted(groups.items()):
            a=chars[0]['account'];line=QHBoxLayout();line.addWidget(QLabel('<b>'+window.alias(a)+'</b>'))
            rename=QPushButton('Rename');rename.clicked.connect(lambda checked=False,a=a:self.rename(a));line.addWidget(rename)
            allb=QPushButton('Select all');allb.clicked.connect(lambda checked=False,chars=chars:self.set_group(chars,True));line.addWidget(allb)
            none=QPushButton('None');none.clicked.connect(lambda checked=False,chars=chars:self.set_group(chars,False));line.addWidget(none)
            self.inner.addLayout(line)
            path_label=QLabel('<span style="color:#92999e">'+escape(path)+'</span>')
            path_label.setWordWrap(True)
            self.inner.addWidget(path_label)
            for c in sorted(chars,key=lambda c:(
                0,int(window.settings['numbers'][c['key']]),str(c['info'].get('name','')).casefold()
            ) if str(window.settings['numbers'].get(c['key'],'')).isdigit() else (
                1,0,str(c['info'].get('name','')).casefold()
            )):
                cb=ManagerCheckBox(str(c['info'].get('name',''))+' - '+str(c['info'].get('realm','')))
                cb.setChecked(c['key'] not in window.settings['hidden'])
                character_line=QHBoxLayout()
                character_line.setSpacing(8)
                character_line.setContentsMargins(0,0,22,0)
                character_line.addWidget(cb,1)
                order=NoWheelComboBox()
                order.addItem('—',0)
                for number in range(1,13):order.addItem(str(number),number)
                order.setToolTip('Character order (— = unnumbered)')
                remove=QToolButton()
                remove.setText('🗑')
                remove.setToolTip('Remove this character from the local list (AlterEgo.lua is untouched)')
                remove.setFixedSize(29,29)
                remove.setStyleSheet('QToolButton{background:#292b2e;color:#f2a6a6;border:1px solid #ffdd2d;border-radius:6px;font-size:16px;} QToolButton:hover{background:#74383e;color:white;border-color:#ffe66d;}')
                remove.clicked.connect(lambda checked=False,key=c['key'],line=character_line:self.remove_character(key,line))
                character_line.addWidget(remove)
                order.setFixedWidth(43)
                order.setStyleSheet('QComboBox{padding:2px 3px;background:#292b2e;color:#ececec;border:1px solid #ffdd2d;border-radius:6px;} QComboBox::drop-down{width:0px;border:0;background:transparent;} QComboBox::down-arrow{image:none;width:0;height:0;}')
                order.setMaxVisibleItems(13)
                popup=QListView(order)
                popup.setUniformItemSizes(True)
                popup.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
                popup.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
                popup.setSpacing(0)
                order.setView(popup)
                popup.setMinimumHeight(13*28+8)
                popup.setStyleSheet('QListView{background:#242629;color:#ececec;border:1px solid #ffdd2d;outline:0;} QListView::item{height:28px;padding-left:12px;} QListView::item:selected{background:#55503a;color:#ffdd2d;}')
                try:current_number=int(window.settings['numbers'].get(c['key']) or 0)
                except (TypeError,ValueError):current_number=0
                index=order.findData(current_number)
                order.setCurrentIndex(index if index>=0 else 0)
                character_line.addWidget(order)
                self.inner.addLayout(character_line)
                self.boxes[c['key']]=cb;self.order_boxes[c['key']]=order
        self.inner.addStretch()
        buttons=QDialogButtonBox(QDialogButtonBox.StandardButton.Save|QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.apply);buttons.rejected.connect(self.reject);lay.addWidget(buttons)
        if window.settings.get('dark_mode',False):
            for widget in [self]+self.findChildren(QWidget):
                css=widget.styleSheet()
                if css:widget.setStyleSheet(theme_css(css,True))
    def remove_character(self,key,line):
        answer=QMessageBox.question(self,'Remove character?',
            'Remove this character from the local list?\n\nThe original AlterEgo.lua will not be changed. You can restore it using Reload all characters.',
            QMessageBox.StandardButton.Yes|QMessageBox.StandardButton.No)
        if answer!=QMessageBox.StandardButton.Yes:return
        self.deleted.add(key)
        self.boxes.pop(key,None)
        self.order_boxes.pop(key,None)
        for i in reversed(range(line.count())):
            item=line.takeAt(i)
            if item.widget():item.widget().deleteLater()
        line.setEnabled(False)

    def reload_all(self):
        self.deleted.clear()
        self.window.settings['deleted']=[]
        self.window.save()
        self.window.refresh()
        self.accept()
        Manager(self.window).exec()

    def set_group(self,chars,checked):
        for c in chars:
            if c['key'] in self.boxes:self.boxes[c['key']].setChecked(checked)
    def rename(self,account):
        w=self.window;s,ok=QInputDialog.getText(self,'Rename Account','Account name:',text=w.alias(account))
        if ok and s.strip():w.settings['aliases'][account]=s.strip();w.save();self.accept();w.refresh();Manager(w).exec()
    def apply(self):
        self.window.settings['deleted']=sorted(self.deleted)
        self.window.settings['hidden']=[key for key,cb in self.boxes.items() if not cb.isChecked()]
        self.window.settings['numbers']={key:spin.currentData() for key,spin in self.order_boxes.items() if spin.currentData()>0}
        self.window.save();self.window.render();self.accept()

class TableIconCache(QWidget):
    """Asynchronous, persistent icons for the main table, without touching Equipment."""
    def __init__(self):
        super().__init__()
        self.manager=QNetworkAccessManager(self)
        self.folder=APP_DIR/'icons'/'table'
        self.waiting={}
        self.pending=set()
        try:self.folder.mkdir(parents=True,exist_ok=True)
        except OSError:pass

    def request_gold(self,label):
        """Fetch the genuine Blizzard MoneyFrame gold denomination texture once.

        The public WoW texture mirror contains converted PNGs. We never use
        the similarly named inventory coin item, nor a hand-drawn substitute.
        """
        key='ui_gold_icon_original'
        label.setProperty('tableIconKey',key)
        path=self.folder/(key+'.png')
        if path.is_file():
            pix=QPixmap(str(path))
            if not pix.isNull():
                label.setPixmap(pix.scaled(14,14,Qt.AspectRatioMode.KeepAspectRatio,
                                           Qt.TransformationMode.SmoothTransformation))
                return
        self.waiting.setdefault(key,[]).append(weakref.ref(label))
        if key in self.pending:return
        self.pending.add(key)
        self._gold_fetch(key,0)

    def _gold_fetch(self,key,index):
        # Try the two standard filename casings from the converted WoW UI mirror.
        urls=(
            'https://raw.githubusercontent.com/Gethe/wow-ui-textures/live/MONEYFRAME/UI-GoldIcon.PNG',
            'https://raw.githubusercontent.com/Gethe/wow-ui-textures/live/MONEYFRAME/UI-GoldIcon.png',
            'https://raw.githubusercontent.com/LoonyMoony/wow-ui-textures-pngonly/master/MONEYFRAME/UI-GoldIcon.PNG',
            'https://raw.githubusercontent.com/LoonyMoony/wow-ui-textures-pngonly/master/MONEYFRAME/UI-GoldIcon.png',
        )
        if index>=len(urls):
            self.pending.discard(key)
            self.waiting.pop(key,None)
            return
        reply=self.manager.get(QNetworkRequest(QUrl(urls[index])))
        reply.finished.connect(lambda k=key,r=reply,n=index:self._gold_done(k,r,n))

    def _gold_done(self,key,reply,index):
        raw=bytes(reply.readAll())
        reply.deleteLater()
        pix=QPixmap()
        if pix.loadFromData(raw) and pix.width()>=8 and pix.height()>=8:
            try:pix.save(str(self.folder/(key+'.png')),'PNG')
            except OSError:pass
            for ref in self.waiting.pop(key,[]):
                label=ref()
                if label is not None and label.property('tableIconKey')==key:
                    label.setPixmap(pix.scaled(14,14,Qt.AspectRatioMode.KeepAspectRatio,
                                               Qt.TransformationMode.SmoothTransformation))
            self.pending.discard(key)
        else:
            self._gold_fetch(key,index+1)

    def request_currency(self,currency_id,label):
        self._request('currency',currency_id,label)

    def request_spell(self,spell_id,label):
        self._request('spell',spell_id,label)

    def _request(self,kind,identifier,label):
        key=kind+'_'+str(identifier)
        label.setProperty('tableIconKey',key)
        path=self.folder/(key+'.png')
        if path.is_file():
            pix=QPixmap(str(path))
            if not pix.isNull():
                label.setPixmap(pix.scaled(19,19,Qt.AspectRatioMode.KeepAspectRatio,Qt.TransformationMode.SmoothTransformation))
                return
        self.waiting.setdefault(key,[]).append(weakref.ref(label))
        if key in self.pending:return
        self.pending.add(key)
        reply=self.manager.get(QNetworkRequest(QUrl('https://nether.wowhead.com/tooltip/'+kind+'/'+str(identifier))))
        reply.finished.connect(lambda k=key,r=reply:self._metadata_done(k,r))

    def _metadata_done(self,key,reply):
        try:
            data=json.loads(bytes(reply.readAll()).decode('utf-8'))
            slug=data.get('icon','') if isinstance(data,dict) else ''
            if isinstance(slug,str):slug=slug.rsplit('/',1)[-1].removesuffix('.jpg').removesuffix('.png')
            if not isinstance(slug,str) or not re.fullmatch(r'[a-zA-Z0-9_]+',slug):
                self.pending.discard(key);return
            req=QNetworkRequest(QUrl('https://wow.zamimg.com/images/wow/icons/large/'+slug+'.jpg'))
            image_reply=self.manager.get(req)
            image_reply.finished.connect(lambda k=key,r=image_reply:self._image_done(k,r))
        except (ValueError,UnicodeDecodeError,TypeError):
            self.pending.discard(key)
        finally:reply.deleteLater()

    def _image_done(self,key,reply):
        raw=bytes(reply.readAll());reply.deleteLater()
        pix=QPixmap()
        if pix.loadFromData(raw) and pix.width()>8:
            try:pix.save(str(self.folder/(key+'.png')),'PNG')
            except OSError:pass
            for ref in self.waiting.pop(key,[]):
                label=ref()
                if label is not None and label.property('tableIconKey')==key:
                    label.setPixmap(pix.scaled(19,19,Qt.AspectRatioMode.KeepAspectRatio,Qt.TransformationMode.SmoothTransformation))
        self.pending.discard(key)

_table_icons=None
def table_icons():
    global _table_icons
    if _table_icons is None:_table_icons=TableIconCache()
    return _table_icons

# Challenge Mode dungeon ID -> dungeon teleport spell ID.
# Spell tooltips expose the ability's actual icon filename.
DUNGEON_TELEPORT_SPELLS={
    588:1286812, # Altar of Fangs
    586:1286807, # Den of Nalorakk
    249:1286831, # King's Rest
    587:1286809, # Murder Row
    399:393256,  # Ruby Life Pools
    250:1286828, # Temple of Sethraliss
    584:1286801, # The Blinding Vale
    585:1286804, # Voidscar Arena
}

CURRENCY_IDS={
    'Adventurer Mistcrest':3442,'Veteran Mistcrest':3443,
    'Champion Mistcrest':3444,'Hero Mistcrest':3445,
    'Myth Mistcrest':3446,'Nebulous Voidcore':3418,
}

# Alternate muted theme. Keep gameplay colours (class, rarity, raid progress)
# unchanged; only UI accents and control borders are recoloured.
THEME_ACCENTS = {
    '#ffdd2d':'#727d88', '#ffe66d':'#9ba6b0', '#ffe06b':'#b4bec6',
    '#c4a82f':'#677580', '#80712e':'#59636b', '#55503a':'#343b42',
    '#454442':'#373e44',
}

def theme_css(css, dark):
    if not dark or not css:return css
    for old,new in THEME_ACCENTS.items():
        css=re.sub(re.escape(old),new,css,flags=re.IGNORECASE)
    return css

class Window(QMainWindow):
    def __init__(self):
        super().__init__();APP_DIR.mkdir(parents=True,exist_ok=True)
        try:self.settings=json.loads(SETTINGS.read_text(encoding='utf-8'))
        except Exception:self.settings={}
        for k,default in [('roots',default_roots()),('hidden',[]),('numbers',{}),('aliases',{}),('collapsed',{}),('window_size',None),('deleted',[]),('dark_mode',False),('refresh_interval_minutes',15)]:self.settings.setdefault(k,default)
        self._first_setup_pending=not SETTINGS.exists() and not bool(self.settings.get('initial_setup_complete',False))
        self.characters=[];self.mtimes={};self.setWindowTitle('AlterEgo Desktop · '+APP_VERSION);self.setWindowIcon(QApplication.instance().windowIcon());self.resize(1360,836)
        self._restoring_window=True
        self.build();self.refresh()
        stored=self.settings.get('window_size')
        if isinstance(stored,list) and len(stored)==2:
            try:
                # One-time adjustment for four section headers, each 6px shorter.
                # Keep the user's saved width and any later manual resizing.
                height=int(stored[1])
                if not self.settings.get('section_header_height_v71_applied'):
                    height-=24
                    self.settings['section_header_height_v71_applied']=True
                    self.save()
                self.resize(max(480,int(stored[0])),max(420,height))
            except (ValueError,TypeError):pass
        self._restoring_window=False
        self._size_save_timer=QTimer(self)
        self._size_save_timer.setSingleShot(True)
        self._size_save_timer.timeout.connect(self._save_window_size)
        # Periodic full reload, even when the source file's mtime is unchanged.
        self.periodic_refresh=QTimer(self)
        self.periodic_refresh.timeout.connect(self.refresh)
        self.periodic_refresh.start(self.refresh_interval_minutes() * 60 * 1000)
        if self._first_setup_pending:
            QTimer.singleShot(250,self.first_run_setup)

    def check_for_updates(self, silent=False):
        self._update_check_silent = bool(silent)
        # Check official GitHub release metadata without blocking the UI.
        repo=UPDATE_REPOSITORY.strip()
        if not re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+',repo):
            QMessageBox.information(self,'Обновления',
                'Проверка обновлений пока не подключена к репозиторию.\n'
                'Разработчик должен указать UPDATE_REPOSITORY в исходном коде.')
            return
        self._update_network=getattr(self,'_update_network',None) or QNetworkAccessManager(self)
        url=QUrl('https://api.github.com/repos/'+repo+'/releases?per_page=30')
        request=QNetworkRequest(url)
        request.setRawHeader(b'Accept',b'application/vnd.github+json')
        request.setRawHeader(b'User-Agent',b'AlterEgo-Desktop-Updater')
        reply=self._update_network.get(request)
        reply.finished.connect(lambda r=reply:self._update_release_result(r))

    @staticmethod
    def _version_tuple(v):
        m=re.match(r'^v?(\d+)\.(\d+)(?:\.(\d+))?$',str(v).strip())
        return tuple(map(int,(m.group(1),m.group(2),m.group(3) or '0'))) if m else None

    def _update_release_result(self,reply):
        try:
            if reply.error()!=QNetworkReply.NetworkError.NoError:
                raise RuntimeError(reply.errorString())
            releases=json.loads(bytes(reply.readAll()).decode('utf-8'))
            if not isinstance(releases,list):
                raise ValueError('GitHub не вернул список релизов')
            valid=[r for r in releases if isinstance(r,dict) and not r.get('prerelease') and not r.get('draft')
                   and self._version_tuple(r.get('tag_name',''))]
            valid.sort(key=lambda r:self._version_tuple(r['tag_name']),reverse=True)
            if not valid:
                if not self._update_check_silent: QMessageBox.information(self,'Обновления','Стабильные обновления пока не опубликованы')
                return
            release=valid[0]
            latest=str(release.get('tag_name','')).strip()
            current=self._version_tuple(APP_VERSION)
            future=self._version_tuple(latest)
            if not current or not future:raise ValueError('Некорректный номер версии в релизе')
            if future<=current:
                if not self._update_check_silent: QMessageBox.information(self,'Обновления','Установлена актуальная версия '+APP_VERSION)
                return
            assets=release.get('assets') or []
            if not isinstance(assets,list): raise ValueError('Некорректный список файлов релиза')
            asset=next((a for a in assets if isinstance(a,dict) and a.get('name') in ('AlterEgo.Desktop.exe','AlterEgo Desktop.exe') and
                        str(a.get('browser_download_url','')).startswith('https://github.com/')),None)
            if not asset:
                QMessageBox.warning(self,'Обновления',f'Версия {latest} опубликована, но файл AlterEgo.Desktop.exe не найден.')
                return
            if not getattr(sys,'frozen',False) or os.name!='nt':
                QMessageBox.information(self,'Обновления',f'Доступна версия {latest}.\nОбновление одной кнопкой работает в Windows EXE.')
                return
            if QMessageBox.question(self,'Доступно обновление',
                    f'Установлена: {APP_VERSION}\nДоступна: {latest}\n\nСкачать и установить обновление?',
                    QMessageBox.StandardButton.Yes|QMessageBox.StandardButton.No)!=QMessageBox.StandardButton.Yes:return
            self._download_update(asset,latest)
        except Exception as exc:
            if not self._update_check_silent: QMessageBox.warning(self,'Ошибка проверки обновлений',str(exc))
        finally:reply.deleteLater()

    def _download_update(self,asset,version):
        self._download_reply=self._update_network.get(QNetworkRequest(QUrl(asset['browser_download_url'])))
        self._download_buffer=bytearray()
        self._download_limit=250*1024*1024
        self._download_dialog=QProgressDialog('Загрузка обновления '+version,'Отмена',0,100,self)
        self._download_dialog.setWindowTitle('AlterEgo Desktop Update')
        self._download_dialog.setMinimumDuration(0)
        self._download_dialog.canceled.connect(self._download_reply.abort)
        self._download_reply.readyRead.connect(self._update_read_chunk)
        self._download_reply.downloadProgress.connect(
            lambda got,total:self._download_dialog.setValue(min(99,int(got*100/total))) if total>0 else None)
        self._download_reply.finished.connect(lambda r=self._download_reply,a=asset:self._finish_update_download(r,a))

    def _update_read_chunk(self):
        self._download_buffer.extend(bytes(self._download_reply.readAll()))
        if len(self._download_buffer)>self._download_limit:self._download_reply.abort()

    def _finish_update_download(self,reply,asset):
        self._update_read_chunk()
        self._download_dialog.close()
        downloaded=None
        try:
            if reply.error()!=QNetworkReply.NetworkError.NoError:
                raise RuntimeError(reply.errorString())
            data=bytes(self._download_buffer)
            if len(data)<1024 or len(data)>self._download_limit or data[:2]!=b'MZ':
                raise ValueError('Загружен некорректный исполняемый файл')
            expected=asset.get('digest','')
            if not isinstance(expected,str) or not re.fullmatch(r'sha256:[a-fA-F0-9]{64}',expected):
                raise ValueError('GitHub не предоставил SHA-256 для проверки файла; обновление отменено')
            if hashlib.sha256(data).hexdigest().lower()!=expected[7:].lower():
                raise ValueError('SHA-256 не совпал; обновление отменено')
            target=Path(sys.executable).resolve()
            staging=APP_DIR/'update'
            staging.mkdir(parents=True,exist_ok=True)
            downloaded=staging/'AlterEgo Desktop.new.exe'
            downloaded.write_bytes(data)
            # An independent PowerShell process waits for this EXE to exit, then
            # atomically replaces it on the same volume. Existing AppData is untouched.
            script=staging/'apply-update.ps1'
            ps=r'''param([int]$ParentPid,[string]$Source,[string]$Target)
$ErrorActionPreference = 'Stop'
$backup = "$Target.before-update.bak"
$installed = $false
try {
  # PyInstaller onefile uses _MEIPASS2/_PYI_* variables for its extracted runtime.
  # A child process launched with these variables may attempt to use the old
  # application's temporary directory after it has been deleted.
  Get-Process -Id $ParentPid -ErrorAction SilentlyContinue | Wait-Process -Timeout 60 -ErrorAction SilentlyContinue
  Start-Sleep -Milliseconds 1000
  foreach ($name in @('_MEIPASS2','_PYI_APPLICATION_HOME_DIR','_PYI_ARCHIVE_FILE','_PYI_PARENT_PROCESS_LEVEL','_PYI_SPLASH_IPC','PYTHONHOME','PYTHONPATH')) {
    [Environment]::SetEnvironmentVariable($name, $null, 'Process')
  }
  if (-not (Test-Path -LiteralPath $Source)) { throw 'Downloaded update is missing' }
  if (-not (Test-Path -LiteralPath $Target)) { throw 'Installed executable is missing' }
  for ($i=0; $i -lt 30; $i++) {
    try {
      if (Test-Path -LiteralPath $backup) {
        $backup = "$Target.before-update.$([DateTime]::UtcNow.ToString('yyyyMMddHHmmss')).bak"
      }
      [System.IO.File]::Move($Target, $backup)
      break
    } catch {
      if ($i -eq 29) { throw }
      Start-Sleep -Milliseconds 500
    }
  }
  try {
    [System.IO.File]::Move($Source, $Target)
    $installed = $true
  } catch {
    [System.IO.File]::Move($backup, $Target)
    throw
  }
  # Start-Process is invoked only after the old executable is fully gone.
  Start-Process -FilePath $Target -WorkingDirectory (Split-Path -Parent $Target)
} catch {
  if ($installed -and (Test-Path -LiteralPath $backup)) {
    try {
      if (Test-Path -LiteralPath $Target) { Remove-Item -LiteralPath $Target -Force }
      [System.IO.File]::Move($backup, $Target)
    } catch {}
  }
  Add-Type -AssemblyName System.Windows.Forms
  [System.Windows.Forms.MessageBox]::Show("Update failed: $($_.Exception.Message)`nBackup: $backup") | Out-Null
}
'''
            script.write_text(ps,encoding='utf-8-sig')
            # Do not propagate PyInstaller's private bootloader environment.
            clean_env=os.environ.copy()
            for name in ('_MEIPASS2','_PYI_APPLICATION_HOME_DIR','_PYI_ARCHIVE_FILE',
                         '_PYI_PARENT_PROCESS_LEVEL','_PYI_SPLASH_IPC',
                         'PYTHONHOME','PYTHONPATH'):
                clean_env.pop(name,None)
            flags=getattr(subprocess,'CREATE_NO_WINDOW',0)
            subprocess.Popen(['powershell.exe','-NoProfile','-ExecutionPolicy','Bypass',
                '-File',str(script),'-ParentPid',str(os.getpid()),'-Source',str(downloaded),'-Target',str(target)],
                creationflags=flags,close_fds=True,env=clean_env)
            QApplication.instance().quit()
        except Exception as exc:
            if downloaded is not None:
                try: downloaded.unlink(missing_ok=True)
                except OSError: pass
            QMessageBox.warning(self,'Обновление не установлено',str(exc))
        finally:
            self._download_buffer=bytearray()
            reply.deleteLater()

    def first_run_setup(self):
        """Offer initial character cleanup only after real data has loaded."""
        if not self._first_setup_pending:return
        if not self.characters:
            answer=QMessageBox.question(self,'Первоначальная настройка',
                'Не удалось найти персонажей AlterEgo.\\n\\n'
                'Укажите папку World of Warcraft или файл AlterEgo.lua. '
                'После загрузки персонажей откроется Manage Characters.',
                QMessageBox.StandardButton.Ok|QMessageBox.StandardButton.Cancel,
                QMessageBox.StandardButton.Ok)
            if answer!=QMessageBox.StandardButton.Ok:return
            path=QFileDialog.getExistingDirectory(self,'Выберите папку WoW, WTF или Account')
            if not path:
                path,_=QFileDialog.getOpenFileName(self,'Выберите AlterEgo.lua','','Lua (*.lua)')
            if not path:return
            if path not in self.settings['roots']:
                self.settings['roots'].append(path);self.save()
            self.refresh()
            if not self.characters:
                QMessageBox.warning(self,'AlterEgo Desktop',
                    'В выбранном расположении персонажи не найдены. '
                    'Попробуйте другую папку через значок папки в верхней панели.')
                return
        if Manager(self).exec()==QDialog.DialogCode.Accepted:
            self.settings['initial_setup_complete']=True
            self._first_setup_pending=False
            self.save()

    def _global_voidcore_season_maximum(self):
        """Return the highest Voidcore Season Maximum among checked characters
        whose source file is recent relative to the newest discovered source.
        Very old SavedVariables files can contain obsolete caps (e.g. 30).
        """
        values=[]
        hidden=set(self.settings.get('hidden',[]))
        file_times=list(self.mtimes.values())
        if not file_times:
            return None
        newest_file_time=max(file_times)
        # Treat files more than seven days behind the newest discovered
        # AlterEgo.lua as stale for this shared, cross-character maximum.
        freshness_window=7*24*60*60
        for character in self.characters:
            if character.get('key') in hidden:
                continue
            source_time=self.mtimes.get(character.get('file'))
            if source_time is None or newest_file_time-source_time > freshness_window:
                continue
            data=obj(character.get('data'))
            for currency in arr(data.get('currencies')):
                if not isinstance(currency,dict):
                    continue
                if currency.get('name')!='Nebulous Voidcore':
                    continue
                try:
                    currency_id=int(currency.get('currencyID') or currency.get('id') or 0)
                except (TypeError,ValueError):
                    continue
                if currency_id!=3418:
                    continue
                # In the supplied AlterEgo data the seasonal maximum is
                # maxQuantity; maxWeeklyQuantity is 0 and is not populated.
                try:
                    parsed=int(currency.get('maxQuantity') or 0)
                except (TypeError,ValueError):
                    continue
                if parsed>0:
                    values.append(parsed)
        return max(values) if values else None

    def eventFilter(self, watched, event):
        if event.type()==QEvent.Type.Wheel and hasattr(self,'content'):
            try:
                inside = watched is self.content or self.content.isAncestorOf(watched)
            except RuntimeError:
                inside = False
            if inside:
                delta=event.angleDelta().y()
                if not delta:
                    delta=event.pixelDelta().y()
                if delta:
                    bar=self.scroll.horizontalScrollBar()
                    bar.setValue(bar.value()-delta)
                    event.accept()
                    return True
        return super().eventFilter(watched,event)

    def _install_table_wheel_filters(self):
        """Route wheel gestures over table cells to horizontal character scrolling."""
        self.content.installEventFilter(self)
        for child in self.content.findChildren(QWidget):
            child.installEventFilter(self)

    def _align_search_field(self):
        # Search input matches Manage Characters + folder + the gap between them.
        # Keep this independent of the current window geometry to avoid
        # resize/layout feedback loops.
        if not hasattr(self,'_search_action_buttons') or not hasattr(self,'search'):
            return
        manage,folder=self._search_action_buttons
        target=manage.sizeHint().width() + 9 + folder.width()
        if target>0 and self.search.width()!=target:
            self.search.setFixedWidth(target)

    def resizeEvent(self,event):
        super().resizeEvent(event)
        if not getattr(self,'_restoring_window',True) and hasattr(self,'_size_save_timer'):
            self._size_save_timer.start(500)
        if hasattr(self,'_frozen_body'):
            QTimer.singleShot(0,self._update_frozen)

    def _save_window_size(self):
        if self.isMinimized() or self.isMaximized():return
        self.settings['window_size']=[self.width(),self.height()]
        self.save()

    def refresh_interval_minutes(self):
        try:
            minutes=int(self.settings.get('refresh_interval_minutes',15))
        except (TypeError,ValueError):
            minutes=15
        return minutes if minutes in (1,5,15,30,60) else 15

    def choose_refresh_interval(self, minutes):
        if minutes not in (1,5,15,30,60): return
        self.settings['refresh_interval_minutes']=minutes
        self.save()
        self.periodic_refresh.start(minutes * 60 * 1000)
        self.refresh_settings_button.setToolTip(
            f'Полная загрузка AlterEgo каждые {minutes} мин. Без проверки изменений каждые 15 секунд.')

    def show_refresh_settings(self):
        menu=QMenu(self)
        dark=bool(self.settings.get('dark_mode',False))
        accent='#727d88' if dark else '#ffdd2d'
        hover='#35404a' if dark else '#454442'
        menu.setStyleSheet(
            'QMenu{background:#242629;color:#eeeeee;border:1px solid '+accent+';'
            'border-radius:7px;padding:6px;}'
            'QMenu::item{padding:7px 22px 7px 12px;border-radius:5px;}'
            'QMenu::item:selected{background:'+hover+';color:#ffffff;}'
            'QMenu::separator{height:1px;background:#45494e;margin:5px 8px;}'
            'QMenu::indicator:checked{background:'+accent+';border-radius:3px;}'
        )
        current=self.refresh_interval_minutes()
        for minutes in (1,5,15,30,60):
            action=menu.addAction(f'{minutes} мин.' + (' (по умолчанию)' if minutes==15 else ''))
            action.setCheckable(True)
            action.setChecked(minutes==current)
            action.triggered.connect(lambda checked=False,m=minutes:self.choose_refresh_interval(m))
        menu.addSeparator()
        info=menu.addAction('Проверка изменений файла отключена')
        info.setEnabled(False)
        menu.exec(self.refresh_settings_button.mapToGlobal(self.refresh_settings_button.rect().bottomLeft()))

    def save(self):SETTINGS.write_text(json.dumps(self.settings,ensure_ascii=False,indent=2),encoding='utf-8')
    def alias(self,account):return self.settings['aliases'].get(account,account)
    def build(self):
        root=QWidget();root.setObjectName('mainTableRoot');self.setCentralWidget(root);l=QVBoxLayout(root);l.setContentsMargins(15,12,15,12);l.setSpacing(9)
        # Two-row header grid keeps the audit buttons exactly beneath the
        # status pill, with no extra vertical row or window-height increase.
        header=QGridLayout()
        header.setContentsMargins(0,0,0,0)
        header.setHorizontalSpacing(9)
        header.setVerticalSpacing(7)
        header.setColumnStretch(1,1)
        brand=QWidget()
        brand_row=QHBoxLayout(brand)
        brand_row.setContentsMargins(0,0,0,0)
        brand_row.setSpacing(12)
        self.brand_label=QLabel()
        brand_row.addWidget(self.brand_label)
        self.dark_toggle=ManagerCheckBox('Я могу один раз ошибиться?')
        self.dark_toggle.setMinimumWidth(self.dark_toggle.sizeHint().width())
        self.dark_toggle.setObjectName('darkModeToggle')
        self.dark_toggle.setChecked(not bool(self.settings.get('dark_mode',False)))
        self.dark_toggle.setToolTip('Checked: yellow accents; unchecked: muted dark theme')
        self.dark_toggle.toggled.connect(self.set_dark_mode)
        brand_row.addWidget(self.dark_toggle)
        header.addWidget(brand,0,0,1,2,Qt.AlignmentFlag.AlignLeft)
        self.status=QLabel()
        self.status.setObjectName('statusSummary')
        self.status.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.status.setMinimumWidth(236)
        self.status.setMinimumHeight(35)
        self.status.setContentsMargins(0,0,0,0)
        self.status.setStyleSheet('QLabel#statusSummary{background:transparent;border:none;color:#ffdd2d;font-weight:600;}')
        header.addWidget(self.status,1,2)
        self.last_refresh_label=QLabel('')
        self.last_refresh_label.setObjectName('refreshSummary')
        self.last_refresh_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.last_refresh_label.setMinimumHeight(35)
        self.last_refresh_label.setContentsMargins(0,0,0,0)
        self.last_refresh_label.setStyleSheet('QLabel#refreshSummary{background:transparent;border:none;color:#ffdd2d;font-weight:600;}')
        self.last_refresh_label.setToolTip('Last successful data reload (computer local time)')
        header.addWidget(self.last_refresh_label,1,3)
        refresh_button=QPushButton('⟳')
        refresh_button.setToolTip('Refresh AlterEgo data')
        refresh_button.setFixedSize(35,35)
        refresh_button.setStyleSheet('QPushButton{background:#292b2e;border:1px solid #ffdd2d;border-radius:7px;padding:0;font-size:25px;font-weight:700;} QPushButton:hover{background:#454442;border-color:#ffe66d;}')
        refresh_button.clicked.connect(self.refresh)
        manage_button=QPushButton('Manage Characters')
        manage_button.clicked.connect(self.manage)
        header.addWidget(manage_button,0,5)
        folder_button=QPushButton('📁')
        folder_button.setFixedSize(35,35)
        folder_button.setToolTip(
            '<b>Папки World of Warcraft</b><br><br>'
            'Здесь указывается, откуда AlterEgo Desktop загружает данные персонажей.<br>'
            'Можно выбрать папку World of Warcraft, папку WTF/Account '
            'или непосредственно файл AlterEgo.lua.<br><br>'
            '<b>Auto-detect</b> — автоматически найти стандартные папки WoW.<br>'
            '<b>Add WoW folder</b> — добавить папку вручную.<br>'
            '<b>Remove folder</b> — убрать папку из списка программы.<br><br>'
            'Файлы игры при этом не изменяются и не удаляются.'
        )
        folder_button.setStyleSheet(
            'QPushButton{background:#292b2e;border:1px solid #ffdd2d;'
            'border-radius:7px;padding:0;font-size:19px;}'
            'QPushButton:hover{background:#454442;border-color:#ffe66d;}'
        )
        folder_button.clicked.connect(self.paths_menu)
        header.addWidget(folder_button,0,6)
        self.refresh_settings_button=QPushButton('⚙')
        self.refresh_settings_button.setFixedSize(35,35)
        self.refresh_settings_button.setToolTip(
            f'Полная загрузка AlterEgo каждые {self.refresh_interval_minutes()} мин. '
            'Без проверки изменений каждые 15 секунд.')
        self.refresh_settings_button.setStyleSheet(
            'QPushButton{background:#292b2e;border:1px solid #ffdd2d;'
            'border-radius:7px;padding:0;font-size:21px;}'
            'QPushButton:hover{background:#454442;border-color:#ffe66d;}')
        self.refresh_settings_button.clicked.connect(self.show_refresh_settings)
        # Two adjacent controls centered as a single group above Updated.
        refresh_controls=QWidget()
        refresh_controls_row=QHBoxLayout(refresh_controls)
        refresh_controls_row.setContentsMargins(0,0,0,0)
        refresh_controls_row.setSpacing(9)
        refresh_controls_row.addWidget(self.refresh_settings_button)
        refresh_controls_row.addWidget(refresh_button)
        header.addWidget(refresh_controls,0,3,Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignVCenter)
        update_button=QPushButton('Update')
        update_button.setToolTip('Проверить обновления AlterEgo Desktop')
        update_button.clicked.connect(self.check_for_updates)
        # Update belongs between the account selector and audit button on row 2.
        header.addWidget(update_button,1,1,Qt.AlignmentFlag.AlignLeft)
        account_group=QWidget()
        account_row=QHBoxLayout(account_group)
        account_row.setContentsMargins(0,0,0,0)
        account_row.setSpacing(7)
        account_row.addWidget(QLabel('Account'))
        self.account=QComboBox()
        self.account.setMinimumWidth(155)
        # Suppress the native dropdown subcontrol background, which paints
        # dark square corners over the rounded combobox border on Windows.
        self.account.setStyleSheet(
            'QComboBox { background:#242629; border:1px solid #ffdd2d;'
            'border-radius:7px; padding:7px 28px 7px 7px; }'
            'QComboBox::drop-down { subcontrol-origin:padding;'
            'subcontrol-position:top right; width:24px;'
            'border:0; background:transparent; }'
            'QComboBox::down-arrow { width:10px; height:10px; }'
        )
        self.account.currentIndexChanged.connect(self.render)
        account_row.addWidget(self.account)
        header.addWidget(account_group,1,0,Qt.AlignmentFlag.AlignLeft)
        audits=QWidget()
        audits_row=QHBoxLayout(audits)
        audits_row.setContentsMargins(0,0,0,0)
        audits_row.setSpacing(9)
        btn=QPushButton('Enchants && Gems Check')
        btn.setToolTip('Check missing enchants and gems for visible characters')
        btn.clicked.connect(lambda checked=False:self.check_equipment('both'))
        audits_row.addWidget(btn,1)
        audits.setFixedWidth(236)
        header.addWidget(audits,0,2)
        search_group=QWidget()
        search_row=QHBoxLayout(search_group)
        search_row.setContentsMargins(0,0,0,0)
        search_row.setSpacing(6)
        search_icon=QLabel('🔍')
        search_icon.setToolTip('Search characters')
        search_icon.setFixedWidth(24)
        search_icon.setAlignment(Qt.AlignmentFlag.AlignCenter)
        search_row.addWidget(search_icon)
        self.search=QLineEdit()
        self.search.setPlaceholderText('Character / Realm')
        # Match Manage Characters + folder, including the 9px gap.
        self.search.setFixedWidth(manage_button.sizeHint().width() + 9 + folder_button.width())
        self.search.setAlignment(Qt.AlignmentFlag.AlignHCenter)
        self.search.textChanged.connect(self.render)
        search_row.addWidget(self.search)
        header.addWidget(search_group,1,4,1,3,Qt.AlignmentFlag.AlignRight)
        # Search icon stays to the left, outside the matching field width.
        self._search_action_buttons=(manage_button,folder_button)
        QTimer.singleShot(0,self._align_search_field)
        l.addLayout(header)
        self.sticky_scroll=QScrollArea();self.sticky_scroll.setWidgetResizable(False);self.sticky_scroll.setFixedHeight(43);self.sticky_scroll.setFrameShape(QFrame.Shape.NoFrame);self.sticky_scroll.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff);self.sticky_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff);l.addWidget(self.sticky_scroll)
        self.sticky_content=QWidget();self.sticky_scroll.setWidget(self.sticky_content);self.sticky_grid=QGridLayout(self.sticky_content);self.sticky_grid.setContentsMargins(0,0,0,0);self.sticky_grid.setHorizontalSpacing(12);self.sticky_grid.setVerticalSpacing(0)
        self.scroll=QScrollArea();self.scroll.setWidgetResizable(False);self.scroll.setFrameShape(QFrame.Shape.NoFrame);self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded);self.scroll.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff);l.addWidget(self.scroll,1)

        # Modern, high-contrast scrollbars: no native Windows arrow buttons.
        scrollbar_style = '''
            QScrollBar:horizontal {
                background:#242629; height:15px; margin:0px;
                border:1px solid #3c3e40; border-radius:7px;
            }
            QScrollBar::handle:horizontal {
                background:#c4a82f; min-width:55px; border-radius:6px;
                margin:2px 2px;
            }
            QScrollBar::handle:horizontal:hover {background:#ffdd2d;}
            QScrollBar::handle:horizontal:pressed {background:#ffe66d;}
            QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal {
                width:0px; height:0px; border:0px; background:none;
            }
            QScrollBar::add-page:horizontal, QScrollBar::sub-page:horizontal {
                background:transparent;
            }
            QScrollBar:vertical {
                background:#242629; width:12px; margin:0px;
                border:1px solid #3c3e40; border-radius:6px;
            }
            QScrollBar::handle:vertical {
                background:#80712e; min-height:35px;
                border-radius:5px; margin:2px;
            }
            QScrollBar::handle:vertical:hover {background:#ffdd2d;}
            QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {
                width:0px; height:0px; border:0px; background:none;
            }
            QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical {
                background:transparent;
            }
        '''
        self.scroll.horizontalScrollBar().setStyleSheet(scrollbar_style)
        self.scroll.verticalScrollBar().setStyleSheet(scrollbar_style)
        self.scroll.horizontalScrollBar().valueChanged.connect(self.sticky_scroll.horizontalScrollBar().setValue)
        self.scroll.horizontalScrollBar().valueChanged.connect(self._update_frozen)
        self.scroll.verticalScrollBar().valueChanged.connect(self._update_frozen)
        self._frozen_body=QWidget(self.scroll.viewport())
        self._frozen_body.setStyleSheet('background:#17191b;')
        self._frozen_body.setFixedWidth(197)
        self._frozen_body.raise_()
        self._frozen_header=QLabel('Character',self.sticky_scroll.viewport())
        self._frozen_header.setFixedHeight(40)
        self._frozen_header.setContentsMargins(7,2,5,2)
        self._frozen_header.setStyleSheet('background:#17191b;color:#ffdd2d;font-weight:700;')
        self._frozen_header.raise_()
        self._frozen_rows=[]
        self.content=QWidget();self.scroll.setWidget(self.content);self.grid=QGridLayout(self.content);self.grid.setContentsMargins(0,0,0,0);self.grid.setHorizontalSpacing(12);self.grid.setVerticalSpacing(0);self.grid.setAlignment(Qt.AlignmentFlag.AlignTop|Qt.AlignmentFlag.AlignLeft)
        self.setStyleSheet("""QWidget{background:#17191b;color:#ececec;font:14px "Segoe UI"} QWidget#mainTableRoot QLabel{font-weight:500} QPushButton{background:#292b2e;border:1px solid #ffdd2d;border-radius:7px;padding:7px 11px;font-weight:600} QPushButton:hover{background:#454442;border-color:#ffe66d} QLineEdit,QComboBox{background:#242629;border:1px solid #ffdd2d;border-radius:7px;padding:7px} QLineEdit:focus,QComboBox:focus{border:1px solid #ffe66d} QLabel#statusPill,QLabel#refreshPill{background:#363b41;border:1px solid #ffdd2d;border-radius:7px;color:#f0f0f0;font-weight:600} QScrollArea{border:0} QToolTip{background:#17171c;color:#eeeeee;border:1px solid #555;padding:8px} QCheckBox{spacing:7px} QDialog{background:#17191b} QTableWidget{background:#202225;gridline-color:#393b3e} QHeaderView::section{background:#292b2d;color:#ffdd2d;padding:7px}""")
        self.apply_theme()
    def set_dark_mode(self, enabled):
        # UI checkbox now enables yellow T-Bank styling; legacy setting
        # stores the inverse (whether muted dark styling is active).
        self.settings['dark_mode']=not bool(enabled)
        self.save()
        self.apply_theme()

    def apply_theme(self):
        dark=bool(self.settings.get('dark_mode',False))
        self.brand_label.setText('<span style="font-size:23px;font-weight:700">AlterEgo '
                                 '<span style="color:'+('#aeb8c0' if dark else '#ffdd2d')+'">Desktop</span></span>')
        # Save each widget's original QSS only once, so switching back
        # restores the exact original styling without cumulative substitutions.
        for widget in [self]+self.findChildren(QWidget):
            original=widget.property('_ae_original_css')
            if original is None:
                original=widget.styleSheet()
                widget.setProperty('_ae_original_css',original)
            if original:
                target=theme_css(original,dark)
                if widget.styleSheet()!=target:widget.setStyleSheet(target)
        # Qt tooltips are top-level popup windows. Apply theme globally, not
        # just to the main window, so Manager/Equipment tooltips match too.
        app=QApplication.instance()
        if app is not None:
            tip_border='#727d88' if dark else '#ffdd2d'
            tip_base='#20252b' if dark else '#17171c'
            palette=QToolTip.palette()
            palette.setColor(QPalette.ColorRole.ToolTipBase,QColor(tip_base))
            palette.setColor(QPalette.ColorRole.ToolTipText,QColor('#eeeeee'))
            QToolTip.setPalette(palette)
            app.setStyleSheet('QToolTip {background-color:'+tip_base+';color:#eeeeee;'
                              'border:2px solid '+tip_border+';border-radius:8px;'
                              'padding:12px;opacity:255;}')
        self.dark_toggle.setChecked(not dark)
        self.dark_toggle.update()
        for heading in self.findChildren(RaidHeaders):
            heading.muted_theme=dark
            heading.update()

    def manage(self):Manager(self).exec()
    def paths_menu(self):
        m=QMenu(self);m.addAction('Add WoW folder',self.add_path);m.addAction('Remove folder',self.remove_path);m.addAction('Auto-detect',self.auto_paths)
        m.exec(self.sender().mapToGlobal(self.sender().rect().bottomLeft()))
    def add_path(self):
        p=QFileDialog.getExistingDirectory(self,'Choose WoW, WTF or Account folder')
        if not p:p,_=QFileDialog.getOpenFileName(self,'Or choose AlterEgo.lua','','Lua (*.lua)')
        if p and p not in self.settings['roots']:self.settings['roots'].append(p);self.save();self.refresh()
    def remove_path(self):
        roots=self.settings['roots']
        if roots:
            s,ok=QInputDialog.getItem(self,'Remove Folder','Path:',roots,0,False)
            if ok:roots.remove(s);self.save();self.refresh()
    def auto_paths(self):
        for p in default_roots():
            if p not in self.settings['roots']:self.settings['roots'].append(p)
        self.save();self.refresh()
    def refresh(self):
        rows=[];seen=set();mtimes={};errors=[]
        for root in self.settings['roots']:
            try:files=find_files(root)
            except OSError as e:errors.append(str(e));continue
            for f in files:
                identity=str(f.resolve()).casefold()
                if identity in seen:continue
                seen.add(identity)
                try:
                    mtimes[str(f)]=f.stat().st_mtime
                    db=load_lua(f)
                    for guid,c in obj(obj(db.get('global')).get('characters')).items():
                        info=obj(obj(c).get('info'))
                        if info.get('name'):rows.append({'key':str(f.resolve())+'|'+str(guid),'guid':str(guid),'account':f.parent.parent.name,'data':c,'info':info,'file':str(f)})
                except Exception as e:errors.append(str(f)+': '+str(e))
        self.characters=rows;self.mtimes=mtimes
        old=self.account.currentData();self.account.blockSignals(True);self.account.clear();self.account.addItem('All Accounts',None)
        for a in sorted({c['account'] for c in rows}):self.account.addItem(self.alias(a),a)
        index=self.account.findData(old);self.account.setCurrentIndex(max(0,index));self.account.blockSignals(False)
        self.render()
        from datetime import datetime
        self.last_refresh_label.setText('Updated ' + datetime.now().strftime('%H:%M:%S'))
        if hasattr(self, 'periodic_refresh'):
            self.periodic_refresh.start(15 * 60 * 1000)
        if errors:QMessageBox.warning(self,'Read errors','\n'.join(errors[:4]))
    def check_equipment(self,kind='both'):
        """Combined audit: enchants first, then gems, per character."""
        chars=self.filtered()
        enchant_slots={'HEADSLOT':'Head','SHOULDERSLOT':'Shoulders',
                       'CHESTSLOT':'Chest','LEGSSLOT':'Legs','FEETSLOT':'Feet',
                       'FINGER0SLOT':'Ring 1','FINGER1SLOT':'Ring 2',
                       'FINGERSLOT':'Ring','MAINHANDSLOT':'Main Hand'}
        findings=[]
        for c in chars:
            equipment=arr(obj(c.get('data')).get('equipment'))
            enchants=[];gems=[];unverified_enchants=[];unverified_gems=[]
            rings=0
            for item in equipment:
                if not isinstance(item,dict):continue
                slot=str(item.get('itemSlotName') or '')
                if slot=='FINGERSLOT':
                    rings+=1
                    slot='FINGER0SLOT' if rings==1 else 'FINGER1SLOT'
                link=str(item.get('itemLink') or '')
                match=re.search(r'\\|Hitem:([^|]+)',link)
                parts=match.group(1).split(':') if match else []
                if slot in enchant_slots:
                    if not parts:
                        unverified_enchants.append(enchant_slots[slot])
                    elif len(parts)<2 or not parts[1].isdigit() or int(parts[1])==0:
                        enchants.append(enchant_slots[slot])
                gem_ids=[x for x in parts[2:6] if x.isdigit() and int(x)>0] if parts else []
                socket_count=None
                for field in ('numSockets','socketCount','socketsCount','gemSocketCount'):
                    value=item.get(field)
                    if isinstance(value,(int,float)) and value>=0:
                        socket_count=int(value);break
                slot_name=GearDialog.LABELS.get(slot,slot)
                if socket_count is not None and len(gem_ids)<socket_count:
                    gems.append(slot_name+f' ({socket_count-len(gem_ids)} empty)')
                elif socket_count is None and slot in ('NECKSLOT','FINGER0SLOT','FINGER1SLOT') and not gem_ids:
                    unverified_gems.append(slot_name)
            if enchants or gems or unverified_enchants or unverified_gems:
                findings.append((c,enchants,gems,unverified_enchants,unverified_gems))

        def character_label(c):
            info=obj(c.get('info'))
            name=str(info.get('name') or 'Unknown')
            realm=str(info.get('realm') or '')
            num=self.settings.get('numbers',{}).get(c['key'])
            prefix=str(num)+'. ' if str(num).isdigit() else ''
            color=character_class_color(info)
            account_name=str(self.alias(c['account']))
            return ('<span style="color:'+color+';font-weight:700">'
                    +escape(prefix+name)+'</span>'
                    +'<span style="color:#aeb6ba"> - '+escape(realm)
                    +' ('+escape(account_name)+')</span>')

        dialog=QDialog(self)
        dialog.setWindowTitle('Enchants & Gems Check')
        dialog.resize(540,470)
        layout=QVBoxLayout(dialog)
        heading=QLabel('Enchants & Gems Check')
        heading.setStyleSheet('font-size:18px;font-weight:700;color:#ffdd2d;')
        layout.addWidget(heading)
        report=QScrollArea()
        report.setWidgetResizable(True)
        content=QWidget()
        stack=QVBoxLayout(content)
        if not chars:
            stack.addWidget(QLabel('No characters match the current filters.'))
        elif not findings:
            ok=QLabel('All Good!')
            ok.setStyleSheet('font-size:21px;color:#5fe09b;font-weight:700;')
            stack.addWidget(ok)
        else:
            confirmed=any(en or gm for _,en,gm,_,_ in findings)
            if not confirmed:
                note=QLabel('No confirmed missing enchants or gems. Some sockets/items cannot be verified.')
                note.setWordWrap(True)
                note.setStyleSheet('color:#ffdd2d;')
                stack.addWidget(note)
            for c,enchants,gems,unverified_enchants,unverified_gems in findings:
                block=QLabel(character_label(c))
                block.setTextFormat(Qt.TextFormat.RichText)
                stack.addWidget(block)
                if enchants:
                    line=QLabel('<b>Missing Enchants:</b> '+escape(', '.join(enchants)))
                    line.setStyleSheet('color:#ff7474;')
                    line.setWordWrap(True)
                    stack.addWidget(line)
                if gems:
                    line=QLabel('<b>Missing Gems:</b> '+escape(', '.join(gems)))
                    line.setStyleSheet('color:#ff7474;')
                    line.setWordWrap(True)
                    stack.addWidget(line)
                if unverified_enchants:
                    line=QLabel('Enchants not verified: '+', '.join(unverified_enchants))
                    line.setStyleSheet('color:#aeb6ba;')
                    line.setWordWrap(True)
                    stack.addWidget(line)
                if unverified_gems:
                    line=QLabel('Sockets not verified: '+', '.join(unverified_gems))
                    line.setStyleSheet('color:#aeb6ba;')
                    line.setWordWrap(True)
                    stack.addWidget(line)
                separator=QFrame()
                separator.setFrameShape(QFrame.Shape.HLine)
                separator.setStyleSheet('color:#45494c;')
                stack.addWidget(separator)
        stack.addStretch(1)
        report.setWidget(content)
        layout.addWidget(report)
        note=QLabel('Gem sockets are checked when socket counts are available in AlterEgo.lua; otherwise they are listed as unverified.')
        note.setWordWrap(True)
        note.setStyleSheet('color:#aeb6ba;')
        layout.addWidget(note)
        close=QPushButton('Close')
        close.clicked.connect(dialog.accept)
        layout.addWidget(close)
        dialog.exec()

    def filtered(self):
        account=self.account.currentData();search=self.search.text().casefold()
        chars=[c for c in self.characters if c['key'] not in self.settings['hidden'] and c['key'] not in self.settings['deleted'] and (not account or c['account']==account) and search in (str(c['info'].get('name',''))+' '+str(c['info'].get('realm',''))).casefold()]
        def key(c):
            n=self.settings['numbers'].get(c['key'])
            return (0,int(n),str(c['info'].get('name','')).lower()) if str(n).isdigit() else (1,0,str(c['info'].get('name','')).lower())
        return sorted(chars,key=key)
    def cleargrid(self):
        while self.grid.count():
            item=self.grid.takeAt(0)
            if item.widget():item.widget().deleteLater()
            elif item.layout():
                layout=item.layout()
                while layout.count():
                    child=layout.takeAt(0)
                    if child.widget():child.widget().deleteLater()
    def cell(self,text,row,col,fg=None,bold=False,click=None,tooltip=None):
        label=QLabel(str(text));label.setFixedHeight(24);label.setContentsMargins(7,1,5,1)
        label.setAlignment(Qt.AlignmentFlag.AlignVCenter | (Qt.AlignmentFlag.AlignCenter if col>0 else Qt.AlignmentFlag.AlignLeft))
        label.setStyleSheet('background:'+('#202427' if row%2 else '#1b1e21')+';color:'+(fg or '#e8e8e8')+';font-weight:'+('700' if bold else ('650' if col>0 else '500'))+';')
        if tooltip:label.setToolTip(tooltip)
        if click:
            label.setCursor(Qt.CursorShape.PointingHandCursor)
            label.mousePressEvent=lambda event:click()
        self.grid.addWidget(label,row,col)
        return label
    def icon_name_cell(self,name,row,kind='currency',identifier=None):
        """Same 26px row as every other main-table cell."""
        cell=QWidget()
        cell.setFixedHeight(24)
        cell.setStyleSheet('background:'+('#202427' if row%2 else '#1b1e21')+';')
        line=QHBoxLayout(cell)
        line.setContentsMargins(7,1,3,1);line.setSpacing(5)
        icon=QLabel()
        icon.setFixedSize(19,19)
        icon.setAlignment(Qt.AlignmentFlag.AlignCenter)
        icon.setStyleSheet('background:transparent;border:0;color:#83b7ce;font-size:10px;font-weight:700;')
        line.addWidget(icon)
        if kind=='currency' and identifier:
            table_icons().request_currency(identifier,icon)
        elif kind=='dungeon' and identifier in DUNGEON_TELEPORT_SPELLS:
            table_icons().request_spell(DUNGEON_TELEPORT_SPELLS[identifier],icon)
        else:
            # Dungeon map IDs are not icon FileDataIDs. Do not use a
            # fabricated item icon; show a compact neutral dungeon symbol.
            icon.setText('◆')
        label=QLabel(name)
        label.setStyleSheet('background:transparent;color:#e8e8e8;font-weight:500;')
        label.setAlignment(Qt.AlignmentFlag.AlignVCenter|Qt.AlignmentFlag.AlignLeft)
        line.addWidget(label,1)
        self.grid.addWidget(cell,row,0)

    def section(self,name,row,cols):
        collapsed=self.settings['collapsed'].get(name,False)
        # A separate header cell for every character keeps the dark vertical gutters
        # continuous through section headers, instead of bridging all columns.
        header_bg='#123341'  # restrained blue-petrol shade inspired by AlterEgo
        for column in range(cols):
            if name=='Raid' and column>0 and not collapsed:
                # Put TG / VA / UoK directly into the Raid section header,
                # eliminating the old empty 26px row below the title.
                headings=RaidHeaders()
                headings.muted_theme=bool(self.settings.get('dark_mode',False))
                headings.on_click=lambda:self.toggle_section('Raid')
                self.grid.addWidget(headings,row,column)
                continue
            label=(('▸  ' if collapsed else '▾  ')+name) if column==0 else ''
            b=QPushButton(label)
            if name=='Great Vault' and column>0:
                character=self._render_chars[column-1]
                if obj(obj(character['data']).get('vault')).get('hasAvailableRewards'):
                    b.setText('Rewards')
            # Section titles are 25% shorter than ordinary 24px data rows.
            b.setFixedHeight(18)
            b.setStyleSheet('text-align:left;background:'+header_bg+';color:'+('#51dc75' if name=='Great Vault' and column>0 and b.text()=='Rewards' else '#ffe06b')+';'
                            'border:0;border-radius:0;padding:0px 6px 2px 6px;font-size:12px;font-weight:700')
            if column>0:b.setStyleSheet(b.styleSheet()+';text-align:center;')
            b.clicked.connect(lambda checked=False,name=name:self.toggle_section(name))
            self.grid.addWidget(b,row,column)
        return not collapsed
    def toggle_section(self,name):self.settings['collapsed'][name]=not self.settings['collapsed'].get(name,False);self.save();self.render()
    def add_widget(self,w,row,col):
        bg='#202427' if row%2 else '#1b1e21'
        if hasattr(w,'row_bg'):w.row_bg=bg
        w.setStyleSheet('background:'+bg+';');self.grid.addWidget(w,row,col)
    def vault_slots(self,c,t):
        slots=arr(obj(obj(c['data']).get('vault')).get('slots'))
        return sorted([x for x in slots if isinstance(x,dict) and x.get('type')==t],key=lambda x:x.get('index',0))[:3]
    def vault_text(self,c,t):
        vals=[]
        for s in self.vault_slots(c,t):
            if (s.get('progress') or 0)<(s.get('threshold') or 1) or not s.get('level'):vals.append(('–','#77818a'));continue
            level=s['level']
            if t==3:
                # Raid vault rewards: level encodes reward difficulty tier.
                # Observed in AlterEgo.lua: 16=Mythic, 15=Heroic.
                tier= ('M','#e49a28') if level>=16 else ('H','#b66bea') if level==15 else ('N','#48ca6a')
            else:
                # Dungeon/world slots use their recorded reward tier; display only unlocked slots.
                tier=(str(level),'#e49a28' if level>=10 else '#b66bea' if level>=9 else '#48ca6a')
            vals.append(tier)
        while len(vals)<3:vals.append(('–','#77818a'))
        return vals
    def vault_widget(self,c,t):
        w=QWidget();w.setObjectName("vaultInner");w.setStyleSheet("QWidget#vaultInner { background: transparent; border: none; }");w.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True);w.setFixedHeight(24);h=QHBoxLayout(w);h.setContentsMargins(6,0,6,0);h.setSpacing(9)
        for text,color in self.vault_text(c,t):
            q=QLabel(text);q.setStyleSheet('color:'+color+';font-weight:750;font-size:15px;background:transparent');h.addWidget(q)
        h.addStretch()
        # Balance both sides so the three vault rewards sit at the center.
        h.insertStretch(0)
        if t in (1,3,6):
            # Immediate tooltip + hover highlight for all three Great Vault activities.
            details='<div style="font-weight:700">'+(vault_raid_tooltip(c) if t==3 else vault_activity_tooltip(c,t))+'</div>'
            class VaultHover(QWidget):
                def __init__(self,inner,details):
                    super().__init__()
                    self.details=details
                    self.setFixedHeight(24)
                    self.setMouseTracking(True)
                    self.hovered=False
                    layout=QHBoxLayout(self);layout.setContentsMargins(0,0,0,0);layout.addWidget(inner)
                    inner.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents,True)
                    self.setAttribute(Qt.WidgetAttribute.WA_Hover,True)
                def paintEvent(self,event):
                    p=QPainter(self)
                    p.fillRect(self.rect(),QColor('#29404a' if self.hovered else '#1b1e21'))
                    p.end()
                    super().paintEvent(event)
                def enterEvent(self,event):
                    self.hovered=True;self.update()
                    QToolTip.showText(self.mapToGlobal(QPoint(self.width()+8,0)),self.details,self)
                    super().enterEvent(event)
                def leaveEvent(self,event):
                    self.hovered=False;self.update()
                    QToolTip.hideText()
                    super().leaveEvent(event)
                def event(self,event):
                    if event.type()==QEvent.Type.ToolTip:
                        QToolTip.showText(self.mapToGlobal(QPoint(self.width()+8,0)),self.details,self)
                        return True
                    return super().event(event)
            return VaultHover(w,details)
        return w
    def clear_sticky(self):
        while self.sticky_grid.count():
            item=self.sticky_grid.takeAt(0)
            if item.widget():item.widget().deleteLater()

    def fit_window_to_content(self,cols):
        # Fit four characters by default; preserve any manually resized window.
        screen=self.screen() or QApplication.primaryScreen()
        available=screen.availableGeometry() if screen else None
        max_width=(available.width()-32) if available else 1600
        max_height=(available.height()-45) if available else 1000
        # Size the window to the actual expanded matrix, leaving a small margin
        # instead of the large unused area produced by the former 1000px height.
        # Keep the size independent of character count and search filtering.
        content_height=max(self.content.sizeHint().height(),self.content.minimumSizeHint().height())
        # Header toolbar + search bar + sticky character header + margins,
        # plus room for the horizontal scrollbar and platform window borders.
        target_height=min(max_height,max(460,content_height+185))
        target_width=min(max_width,185+4*232+65)
        if not getattr(self,"_initial_fit_done",False):self._locked_table_height=int(target_height)
        if not getattr(self,'_initial_fit_done',False):
            self.resize(int(target_width),int(getattr(self,"_locked_table_height",target_height)))
        self._initial_fit_done=True
    def update_status(self):
        # A removed character stays in the source Lua but should not be counted.
        # Hidden (unchecked) characters are still present, not deleted.
        deleted=set(self.settings.get('deleted',[]))
        kept=[c for c in self.characters if c['key'] not in deleted]
        count=len(kept)
        accounts=len({c['account'] for c in kept})
        self.status.setText(f'{count} Characters  ·  {accounts} Accounts')

    def render(self,*args):
        if not hasattr(self,'grid'):return
        self.update_status()
        # Drop old overlay labels only after the new render has been laid out.
        old_frozen=getattr(self,'_frozen_rows',[])
        self._frozen_rows=[]
        for widget,_ in old_frozen:
            widget.hide()
            widget.deleteLater()
        self.cleargrid();self.clear_sticky()
        # QGridLayout retains its former row count after cleargrid(), which
        # leaves a large blank table after Save. Replace the widgets/layouts.
        old_content=self.content
        old_sticky=self.sticky_content
        self.content=QWidget()
        self.grid=QGridLayout(self.content)
        self.grid.setContentsMargins(0,0,0,0)
        self.grid.setHorizontalSpacing(12)
        self.grid.setVerticalSpacing(0)
        self.grid.setAlignment(Qt.AlignmentFlag.AlignTop|Qt.AlignmentFlag.AlignLeft)
        # Keep the newly created widget outside QScrollArea until its layout
        # has been populated. Qt may hide an empty widget installed too early.
        self.sticky_content=QWidget()
        self.sticky_grid=QGridLayout(self.sticky_content)
        self.sticky_grid.setContentsMargins(0,0,0,0)
        self.sticky_grid.setHorizontalSpacing(12)
        self.sticky_grid.setVerticalSpacing(0)
        # Both new widgets are installed only after their cells are ready.
        # QScrollArea.setWidget() already destroys the previous widget.
        # Never call deleteLater() on its stale Python wrapper.
        chars=self.filtered();self._render_chars=chars;cols=len(chars)+1
        self.content.setMinimumSize(0,0)
        self.content.setMaximumWidth(16777215)
        self.sticky_content.setMinimumSize(0,0)
        for column in range(self.grid.columnCount()):self.grid.setColumnMinimumWidth(column,0)
        self.grid.setColumnMinimumWidth(0,185)
        for column in range(1,cols):self.grid.setColumnMinimumWidth(column,CHAR_COLUMN_WIDTH)
        sticky_title=QLabel('Character');sticky_title.setFixedHeight(40);sticky_title.setContentsMargins(7,2,5,2);sticky_title.setStyleSheet('background:#1b1e21;color:#ffdd2d;font-weight:700');self.sticky_grid.addWidget(sticky_title,0,0)
        for i,c in enumerate(chars,1):
            number=self.settings['numbers'].get(c['key']);prefix=(str(number)+'. ') if number is not None else ''
            name=str(c['info'].get('name',''))
            realm=str(c['info'].get('realm',''))
            gold=int(obj(c['data']).get('money') or 0)//10000
            class CharacterHeader(QFrame):
                def __init__(self, character, parent_window):
                    super().__init__()
                    self.character=character
                    self.parent_window=parent_window
                    self.setFixedHeight(40)
                    self.setCursor(Qt.CursorShape.PointingHandCursor)
                    self.setObjectName('characterHeader')
                    self.setStyleSheet(
                        'QFrame#characterHeader{background:#1b1e21;border:0;}'
                        'QFrame#characterHeader:hover{background:#29404a;border:0;}'
                        'QFrame#characterHeader QLabel{background:transparent;border:0;}'
                    )
                def mousePressEvent(self,event):
                    if event.button()==Qt.MouseButton.LeftButton:
                        GearDialog(self.character,self.parent_window).exec()
                    else:
                        super().mousePressEvent(event)
            header=CharacterHeader(c,self)
            line=QHBoxLayout(header);line.setContentsMargins(7,2,7,2);line.setSpacing(3)
            name_stack=QVBoxLayout();name_stack.setContentsMargins(0,0,0,0);name_stack.setSpacing(0)
            class_color=character_class_color(c['info'])
            nick=QLabel('<span style="color:'+class_color+';font-weight:700">'+escape(prefix+name)+'</span>'
                        '<span style="font-size:12px;color:#9da4a8"> - '+escape(realm)+'</span>')
            nick.setTextFormat(Qt.TextFormat.RichText)
            nick.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents,True)
            name_stack.addWidget(nick)
            account_name=QLabel(escape(self.alias(c['account'])))
            account_name.setTextFormat(Qt.TextFormat.RichText)
            account_name.setStyleSheet('color:#8898a2;font-size:12px;font-weight:500;background:transparent;')
            account_name.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents,True)
            name_stack.addWidget(account_name)
            line.addLayout(name_stack,0)
            line.addStretch(1)
            money=QLabel(format(gold,','))
            money.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents,True)
            money.setStyleSheet('background:transparent;color:#e9c36c;font-size:12px;font-weight:700;')
            line.addWidget(money,0)
            coin=QLabel('g');coin.setFixedSize(16,16)
            coin.setAlignment(Qt.AlignmentFlag.AlignCenter)
            coin.setStyleSheet('background:transparent;color:#e9c36c;font-size:12px;')
            coin.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents,True)
            line.addWidget(coin,0)
            table_icons().request_gold(coin)
            self.sticky_grid.addWidget(header,0,i)
        row=0
        def simple(label,fn,color=None,value_color=None,bold_value=False):
            nonlocal row
            self.cell(label,row,0,'#ffdd2d' if color else '#ededed',bool(color))
            for i,c in enumerate(chars,1):self.cell(fn(c),row,i,fg=value_color(c) if value_color else None,bold=bold_value)
            row+=1
        simple('Item Level',lambda c:(f"{float(obj(c['info'].get('ilvl')).get('equipped')):.1f}" if obj(c['info'].get('ilvl')).get('equipped') is not None else '—'),value_color=minimum_equipment_track,bold_value=True)
        simple('M+ Rating',lambda c:fmt(obj(obj(c['data']).get('mythicplus')).get('rating')),value_color=lambda c:rating_color(obj(obj(c['data']).get('mythicplus')).get('rating')))
        def keystone(c):
            k=obj(obj(obj(c['data']).get('mythicplus')).get('keystone'))
            try:level=int(k.get('level') or 0)
            except (TypeError,ValueError):level=0
            if level<=0:return '–'
            link=str(k.get('itemLink') or '')
            # The link carries the actual owned keystone's dungeon name.
            match=re.search(r'\\[Keystone:\\s*(.*?)\\s*\\(\\d+\\)\\]',link,re.I)
            if match:name=match.group(1).strip()
            else:
                dungeons={588:'Altar of Fangs',586:'Den of Nalorakk',249:"Kings’ Rest",
                          587:'Murder Row',399:'Ruby Life Pools',250:'Temple of Sethraliss',
                          584:'The Blinding Vale',585:'Voidscar Arena'}
                try:cid=int(k.get('challengeModeID'))
                except (TypeError,ValueError):cid=None
                name=dungeons.get(cid) or str(k.get('name') or 'Unknown Dungeon')
            return name+' +'+str(level)
        simple('Current Keystone',keystone)
        if self.section('Great Vault',row,cols):
            row+=1
            # SavedVariables type IDs: 3=Raids (2/4/6), 1=Dungeons (1/4/8), 6=World (2/4/8).
            for label,t in [('Raids',3),('Dungeons',1),('World',6)]:
                self.cell(label,row,0)
                for i,c in enumerate(chars,1):self.add_widget(self.vault_widget(c,t),row,i)
                row+=1
        else:row+=1
        if self.section('Raid',row,cols):
            row+=1
            for difficulty,color in DIFFS:
                self.cell(difficulty,row,0)
                for i,c in enumerate(chars,1):self.add_widget(Skulls(c,difficulty),row,i)
                row+=1
        else:row+=1
        if self.section('Dungeons',row,cols):
            row+=1
            ids={588:'Altar of Fangs',586:'Den of Nalorakk',249:"Kings’ Rest",587:'Murder Row',399:'Ruby Life Pools',250:'Temple of Sethraliss',584:'The Blinding Vale',585:'Voidscar Arena'}
            for did,name in ids.items():
                self.icon_name_cell(name,row,'dungeon',did)
                for i,c in enumerate(chars,1):
                    dungeon=next((d for d in arr(obj(obj(c['data']).get('mythicplus')).get('dungeons'))
                                  if isinstance(d,dict) and d.get('challengeModeID')==did),None)
                    d=obj(dungeon)
                    if d.get('level'):
                        try: rating=str(round(float(d.get('rating') or 0)))
                        except (ValueError,TypeError): rating='—'
                        level_text=escape(str(d['level']))
                        value=('<span style="color:'+keystone_level_color(d['level'])+'">'+level_text+'</span>'
                               '<span style="color:#9ba6ae">  ·  </span>'
                               '<span style="color:'+keystone_level_color(d['level'])+'">'+escape(rating)+'</span>')
                    else:
                        value='–'
                    bg='#202427' if row%2 else '#1b1e21'
                    details=dungeon_tooltip(name,dungeon) if d.get('level') else ''
                    self.grid.addWidget(HoverCurrency(value,details,bg),row,i)
                row+=1
        else:row+=1
        if self.section('Currencies',row,cols):
            row+=1
            names=sorted({x.get('name','') for c in chars for x in arr(obj(c['data']).get('currencies')) if isinstance(x,dict) and ('Mistcrest' in x.get('name','') or 'Nebulous Voidcore' in x.get('name',''))})
            for name in names:
                def amount(c,name=name):
                    matches=[x for x in arr(obj(c['data']).get('currencies')) if isinstance(x,dict) and x.get('name')==name]
                    return str(matches[0].get('quantity',0)) if matches else '–'
                self.icon_name_cell(name,row,'currency',CURRENCY_IDS.get(name))
                for i,c in enumerate(chars,1):
                    currency=next((x for x in arr(obj(c['data']).get('currencies'))
                                   if isinstance(x,dict) and x.get('name')==name),None)
                    bg='#202427' if row%2 else '#1b1e21'
                    season_maximum=(self._global_voidcore_season_maximum()
                                    if name=='Nebulous Voidcore' else None)
                    value_widget=HoverCurrency(
                        amount(c),crest_tooltip(currency,season_maximum),bg,
                        text_color='#ff5555' if season_currency_capped(currency,season_maximum) else None)
                    value_widget.set_currency_icon(CURRENCY_IDS.get(name))
                    self.grid.addWidget(value_widget,row,i)
                row+=1
        else:row+=1
        # No expanding spacer row: keep the Raid section flush with Dungeons.
        # Ensure the rightmost character is fully reachable via a horizontal scrollbar.
        # Reserve width for each character, including an inter-character gutter.
        required_width = 185 + len(chars)*CHAR_COLUMN_WIDTH + max(0,cols-1)*12
        self.content.setFixedWidth(required_width)
        self.grid.activate()
        # Never allow a zero/one-pixel body on a rebuild. A detached grid's
        # size hint can temporarily be empty before the Qt layout pass.
        row_heights=[]
        for r in range(self.grid.rowCount()):
            heights=[self.grid.itemAtPosition(r,c).sizeHint().height()
                     for c in range(cols) if self.grid.itemAtPosition(r,c)]
            row_heights.append(max(heights,default=24))
        body_height=max(self.grid.sizeHint().height(),sum(row_heights),24)
        self.content.setFixedHeight(body_height)
        # Installing populated widgets makes them visible on the first frame
        # and after Save/Search, without restarting the application.
        self.scroll.setWidget(self.content)
        self.sticky_scroll.setWidget(self.sticky_content)
        self._install_table_wheel_filters()
        self.content.show()
        self.sticky_content.show()
        self.sticky_grid.setColumnMinimumWidth(0,185)
        for column in range(1,cols):self.sticky_grid.setColumnMinimumWidth(column,CHAR_COLUMN_WIDTH)
        self.sticky_content.setFixedSize(required_width,40)
        self.sticky_scroll.horizontalScrollBar().setRange(0,max(0,required_width-self.sticky_scroll.viewport().width()))
        self.sticky_scroll.horizontalScrollBar().setValue(self.scroll.horizontalScrollBar().value())
        self.fit_window_to_content(cols)
        # The first column is a fixed-width overlay, and all character
        # columns share the same 220px width and 12px gap.
        self._freeze_left_column()
        self.apply_theme()

    def _freeze_left_column(self):
        # Move only the left-hand label widgets out of the horizontally
        # scrolling content. Preserve empty grid cells as column-width anchors.
        self.grid.activate()
        for row in range(self.grid.rowCount()):
            item=self.grid.itemAtPosition(row,0)
            if not item or not item.widget():continue
            widget=item.widget()
            # Preserve the original grid cell dimensions after moving the label.
            # Without this spacer Qt collapses the first column and shifts all
            # character data left underneath the fixed overlay.
            width=185
            # Match the row height on the character side exactly. In
            # particular, section headers are 18px, not the old 24px.
            # widget.height() can still reflect its pre-layout geometry.
            height=18 if (isinstance(widget,QPushButton) and
                          widget.text().lstrip('▸▾  ').strip() in
                          ('Great Vault','Raid','Dungeons','Currencies')) else max(1, self.grid.cellRect(row,0).height())
            self.grid.removeWidget(widget)
            spacer=QWidget()
            spacer.setFixedSize(width,height)
            spacer.setStyleSheet('background:transparent;border:0;')
            self.grid.addWidget(spacer,row,0)
            widget.setParent(self._frozen_body)
            widget.setMinimumHeight(0)
            widget.setMaximumHeight(16777215)
            widget.show()
            self._frozen_rows.append((widget,row))
        self.align_character_headers()
        self._update_frozen()

    def _update_frozen(self,*args):
        if not hasattr(self,'_frozen_body'):return
        view=self.scroll.viewport()
        self._frozen_body.setGeometry(0,0,197,view.height())
        self._frozen_body.raise_()
        self._frozen_header.setGeometry(0,0,197,self.sticky_scroll.viewport().height())
        self._frozen_header.raise_()
        if not self._frozen_rows:return
        self.grid.activate()
        offset=self.scroll.verticalScrollBar().value()
        for widget,row in self._frozen_rows:
            rect=self.grid.cellRect(row,0)
            widget.setGeometry(0,rect.y()-offset,185,rect.height())
            widget.setVisible(rect.bottom()>=offset and rect.y()-offset<view.height())

    def align_character_headers(self):
        if not hasattr(self,'grid') or not hasattr(self,'sticky_grid'):return
        self.grid.activate()
        for col in range(self.sticky_grid.columnCount()):
            item=self.sticky_grid.itemAtPosition(0,col)
            if item and item.widget():
                item.widget().setFixedWidth(185 if col==0 else CHAR_COLUMN_WIDTH)
        self.sticky_grid.activate()
        width=self.content.width()
        self.sticky_content.setFixedSize(width,40)
        self.sticky_scroll.horizontalScrollBar().setValue(self.scroll.horizontalScrollBar().value())

if __name__=='__main__':
    app=QApplication(sys.argv)
    # The PyInstaller --icon flag sets the EXE file icon, not the Qt window/taskbar icon.
    # PyInstaller bundles this .ico in _MEIPASS for the running GUI.
    icon_path=Path(getattr(sys,'_MEIPASS',Path(__file__).resolve().parent))/'app.ico'
    if icon_path.is_file():app.setWindowIcon(QIcon(str(icon_path)))
    # Apply the same dark palette to every Qt tooltip, including HTML tooltips.
    # Setting this on QApplication is important because tooltip windows are
    # independent top-level Qt windows, not children of the main window.
    tooltip_palette=QToolTip.palette()
    tooltip_palette.setColor(QPalette.ColorRole.ToolTipBase, QColor('#17171c'))
    tooltip_palette.setColor(QPalette.ColorRole.ToolTipText, QColor('#eeeeee'))
    tooltip_palette.setColor(QPalette.ColorRole.Window, QColor('#17171c'))
    tooltip_palette.setColor(QPalette.ColorRole.WindowText, QColor('#eeeeee'))
    QToolTip.setPalette(tooltip_palette)
    try:
        window=Window();window.show();sys.exit(app.exec())
    except Exception:
        error=traceback.format_exc();(APP_DIR/'error.log').write_text(error,encoding='utf-8')
        QMessageBox.critical(None,'AlterEgo Desktop — error',error)
