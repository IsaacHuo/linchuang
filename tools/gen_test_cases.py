"""批量生成合成测试病例（教学用，非真实患者）。

  python tools/gen_test_cases.py --n 500 --seed 2026

输出（符合 input.json 协议）：
  data/test_cases_<n>.json        病例
  data/test_cases_<n>_meta.json   每例的场景、陷阱类型、资料中出现的数值、明确缺失项、入院初步判断，
                                  供自动评测与人工抽查使用

病例由 20 类常见急症模板 + 场景内典型异常范围的随机数值 + 随机陷阱组合而成。陷阱包括：
院外已实施治疗、同一检验两次结果冲突、发病时间说法不一、生命体征缺项、过敏史、人口学信息不全、资料极少。
同一 seed 生成结果完全一致。医学合理性需指导教师抽查。
"""
import argparse
import json
import random
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCEN = []


def scenario(name, focus, age, sex=None):
    def deco(fn):
        SCEN.append(dict(name=name, focus=focus, age=age, sex=sex, build=fn))
        return fn
    return deco


class Case:
    def __init__(self, r):
        self.r = r
        self.sex = r.choice("男女")
        self.age = 50
        self.hr = 80
        self.numbers = []
        self.missing = []
        self.labs = {}
        self.traps = []

    def i(self, lo, hi):
        return self.r.randint(lo, hi)

    @staticmethod
    def fmt(v, d):
        return f"{v:.{d}f}" if d else str(int(round(v)))

    @staticmethod
    def unit(v, u):
        return f"{v}{'' if u == '%' else ' '}{u}"

    def n(self, lo, hi, u, d=0):
        s = self.unit(self.fmt(self.r.uniform(lo, hi), d), u)
        self.numbers.append(s)
        return s

    def pick(self, xs):
        return self.r.choice(xs)

    def some(self, xs, lo, hi):
        xs = list(xs)
        return self.r.sample(xs, min(len(xs), self.i(lo, hi)))

    def lab(self, name, lo, hi, u, d=0, ref=""):
        v = self.r.uniform(lo, hi)
        self.labs[name] = (v, u, d)
        s = self.unit(self.fmt(v, d), u)
        self.numbers.append(s)
        return f"{name} {s}{ref}"

    def vitals(self, t=(36.3, 37.2), hr=(70, 95), rr=(16, 20), sbp=(110, 135), dbp=(65, 85),
               spo2=(95, 99), irregular=False):
        drop = None
        if self.r.random() < 0.3:
            drop = self.pick(["体温", "呼吸频率", "指脉氧饱和度"])
            self.missing.append(drop)
            self.traps.append("生命体征缺项")
        out = []
        if drop != "体温":
            out.append("体温 " + self.n(*t, "℃", 1))
        self.hr = self.i(*hr)
        s = f"{self.hr} 次/分"
        self.numbers.append(s)
        out.append("心率 " + s + ("，心律绝对不齐" if irregular else ""))
        if drop != "呼吸频率":
            out.append("呼吸 " + self.n(*rr, "次/分"))
        sb = self.i(*sbp)
        db = min(self.i(*dbp), sb - 25)
        s = f"{sb}/{db} mmHg"
        self.numbers.append(s)
        out.append("血压 " + s)
        if drop != "指脉氧饱和度":
            out.append("未吸氧状态下指脉氧饱和度 " + self.n(*spo2, "%"))
        return "，".join(out) + (f"；未提供{drop}" if drop else "")


WBC = ("白细胞", "×10^9/L", 1)
HIGH, LOW = "（实验室标高）", "（实验室标低）"


def wbc(c, lo, hi):
    return c.lab("白细胞", lo, hi, "×10^9/L", 1, HIGH if lo >= 10 else "")


# ---------------- 20 类场景 ----------------

@scenario("acs", "胸痛：肌钙蛋白升高但不能直接确诊", (42, 82))
def acs(c):
    vit = c.vitals(hr=(60, 105), sbp=(110, 175), dbp=(70, 100))
    return dict(
        cc=(f"阵发性胸骨后压榨样疼痛 {c.i(1, 7)} 天，每次持续约 {c.i(3, 15)} 分钟，休息后可缓解；"
            f"{c.pick(['今晨', '今日午后', '今晚'])}疼痛加重，持续约 {c.i(20, 60)} 分钟，"
            f"伴{c.pick(['出汗', '恶心', '左肩部放射痛', '出汗、恶心'])}"),
        hist=[f"高血压 {c.i(3, 20)} 年，口服{c.pick(['氨氯地平', '缬沙坦', '硝苯地平控释片'])}，剂量未提供",
              f"吸烟 {c.i(10, 40)} 年，每日约 {c.pick([10, 20, 30])} 支",
              f"2 型糖尿病 {c.i(2, 15)} 年，口服二甲双胍", "否认既往冠心病史"],
        hist_miss=["血脂情况", "家族早发冠心病史", "既往心电图"],
        vit=vit, signs=["双肺呼吸音清", "心律齐"], neg=["各瓣膜听诊区未闻及杂音", "双下肢无水肿"],
        labs=[c.lab("肌钙蛋白 I", 0.05, 1.5, "ng/mL", 2, "（实验室参考上限 0.04 ng/mL）"),
              c.lab("肌酐", 60, 110, "μmol/L")],
        labs_miss=["复查肌钙蛋白", "超声心动图", "血脂", "凝血功能"],
        extra=[c.pick(["心电图：V1—V4 导联 ST 段压低约 0.1 mV", "心电图：II、III、aVF 导联 T 波倒置",
                       "心电图：窦性心律，未见明显 ST 段抬高"])],
        imp=c.pick(["胸痛待查，急性冠脉综合征可能", "急性非 ST 段抬高型心肌梗死可能"]),
        outside=["院外自行含服硝酸甘油 1 片后疼痛部分缓解", "院外已嚼服阿司匹林 300 mg"],
        conflict=["肌钙蛋白 I"])


@scenario("pneumonia", "发热咳嗽：区分院外治疗与入院计划", (20, 88))
def pneumonia(c):
    side = c.pick(["右下", "左下", "右上"])
    vit = c.vitals(t=(37.8, 39.4), hr=(90, 118), rr=(20, 28), spo2=(88, 96))
    return dict(
        cc=(f"发热伴咳嗽、咳{c.pick(['黄痰', '白黏痰', '黄脓痰'])} {c.i(2, 7)} 天，最高体温 {c.n(38.2, 39.8, '℃', 1)}"
            + c.pick(["", "；近 2 天出现活动后气促", "，伴右侧胸痛，深吸气时加重"])),
        hist=[f"高血压 {c.i(2, 15)} 年", f"2 型糖尿病 {c.i(2, 15)} 年", f"慢性阻塞性肺疾病病史 {c.i(2, 10)} 年", "否认结核病史"],
        hist_miss=["疫苗接种史", "近期住院及抗生素使用史", "吸烟史"],
        vit=vit, signs=[f"{side}肺可闻及湿啰音"], neg=["无颈静脉怒张", "双下肢无水肿", "无明显发绀"],
        labs=[wbc(c, 10.5, 19.5), c.lab("中性粒细胞百分比", 78, 92, "%", 1),
              c.lab("C 反应蛋白", 25, 180, "mg/L"), c.lab("降钙素原", 0.15, 4.5, "ng/mL", 2)],
        labs_miss=["血气分析", "痰培养", "血培养", "既往胸部影像"],
        extra=[f"胸部 {c.pick(['X 线', 'CT'])}：{side}肺斑片状渗出影"],
        imp="社区获得性肺炎可能",
        outside=["院外口服阿莫西林 3 天，剂量未提供，体温无明显下降", "院外静脉输注抗生素 2 天，具体药物未提供"],
        conflict=["白细胞"])


@scenario("appendicitis", "腹痛：典型病例与有鉴别价值的阴性", (15, 60))
def appendicitis(c):
    vit = c.vitals(t=(37.2, 38.6), hr=(80, 108))
    return dict(
        cc=(f"转移性右下腹痛 {c.i(8, 36)} 小时，起初为脐周隐痛，后转移至右下腹并持续加重，伴恶心"
            + c.pick(["", f"、呕吐 {c.i(1, 3)} 次"])),
        hist=["既往体健", "否认腹部手术史", f"慢性胃炎病史 {c.i(1, 10)} 年"],
        hist_miss=["排便情况", "药物史"], preg=True,
        vit=vit, signs=["右下腹麦氏点压痛、反跳痛"], neg=["无明显肌紧张", "墨菲征阴性", "双肾区无叩痛"],
        labs=[wbc(c, 10.5, 17.5), c.lab("中性粒细胞百分比", 78, 90, "%", 1), c.lab("C 反应蛋白", 8, 90, "mg/L")],
        labs_miss=["尿常规", "腹部 CT", "凝血功能"],
        extra=[c.pick([f"腹部超声：右下腹探及盲端管状结构，直径约 {c.n(7, 12, 'mm')}，周围少量渗出",
                       "腹部超声：阑尾显示不清"])],
        imp=c.pick(["急性阑尾炎可能", "右下腹痛待查，急性阑尾炎可能"]),
        outside=["院外自行服用止痛药 1 次，药名不详"], conflict=["白细胞"])


@scenario("dka", "糖尿病酮症酸中毒：数值密集、诱因未明", (16, 65))
def dka(c):
    vit = c.vitals(t=(36.4, 37.8), hr=(100, 128), rr=(22, 30), sbp=(95, 125), dbp=(55, 80))
    return dict(
        cc=f"口干、多饮、多尿 {c.i(3, 14)} 天，伴恶心、呕吐、乏力 {c.i(1, 3)} 天",
        hist=[[f"1 型糖尿病 {c.i(2, 15)} 年，皮下注射胰岛素，近 {c.i(2, 5)} 天自行停用",
               f"2 型糖尿病 {c.i(1, 10)} 年，口服降糖药，具体药物未提供", "既往未诊断糖尿病"],
              "近 1 周有咽痛、咳嗽", "否认胰腺炎病史"],
        hist_miss=["胰岛素剂量", "既往糖化血红蛋白", "近期饮食情况"],
        vit=vit, signs=["呼吸深快", "皮肤及口唇干燥"], neg=["腹部无明显压痛", "神志清楚"],
        labs=[c.lab("血糖", 18, 34, "mmol/L", 1, HIGH), c.lab("血β-羟丁酸", 3.0, 7.5, "mmol/L", 1),
              f"动脉血气 pH {c.fmt(c.r.uniform(7.05, 7.28), 2)}", c.lab("碳酸氢根", 5, 15, "mmol/L", 1),
              c.lab("血钾", 3.2, 5.9, "mmol/L", 1), c.lab("血钠", 128, 140, "mmol/L")],
        labs_miss=["尿酮体", "血渗透压", "糖化血红蛋白", "血淀粉酶"],
        imp="糖尿病酮症酸中毒可能，诱因待查",
        outside=["院外自测指尖血糖仪显示“HI”，未处理", "院外自行皮下注射胰岛素 1 次，剂量不详"],
        conflict=["血钾", "血糖"])


@scenario("stroke", "卒中：发病时间不确定、定位与侧别", (50, 88))
def stroke(c):
    side = c.pick(["右", "左"])
    other = "左" if side == "右" else "右"
    vit = c.vitals(sbp=(150, 200), dbp=(85, 110), hr=(62, 100))
    return dict(
        cc=(f"{c.pick(['晨起时', '午睡醒后', '家属发现'])}{side}侧肢体无力伴"
            f"{c.pick(['言语不清', '口角歪斜', '言语不清、口角歪斜'])}，最后看起来正常的时间为"
            f"{c.pick(['昨晚 22 时左右', '今晨 6 时左右', '约 4 小时前'])}"),
        time_conflict=[f"家属另诉患者昨日傍晚即有{side}手麻木，具体时间说法不一",
                       "急诊分诊记录发病时间为“约 2 小时前”，与家属描述不一致"],
        hist=[f"高血压 {c.i(3, 20)} 年，服药不规律",
              [f"心房颤动 {c.i(1, 8)} 年，抗凝药使用情况未提供", "否认心房颤动病史"],
              f"2 型糖尿病 {c.i(2, 15)} 年", f"吸烟 {c.i(10, 40)} 年"],
        hist_miss=["抗血小板及抗凝药物使用情况", "既往卒中史", "近期手术及出血史"],
        vit=vit,
        signs=[f"{side}侧上肢肌力 {c.i(1, 4)} 级，{side}侧下肢肌力 {c.i(2, 4)} 级", f"{side}侧巴宾斯基征阳性"],
        neg=["颈软，无抵抗", "双侧瞳孔等大等圆，对光反射灵敏"],
        labs=[c.lab("血糖", 4.8, 11, "mmol/L", 1), c.lab("血小板", 120, 320, "×10^9/L")],
        labs_miss=["凝血功能", "头颅 MRI", "NIHSS 评分", "心电图"],
        extra=[c.pick(["头颅 CT：未见颅内出血", f"头颅 CT：{other}侧基底节区低密度影，未见出血"])],
        imp=f"急性缺血性脑卒中可能（{other}侧大脑半球）", conflict=["血糖"])


@scenario("ugib", "消化道出血：血红蛋白冲突、活动性出血评估", (30, 85))
def ugib(c):
    vit = c.vitals(hr=(92, 122), sbp=(86, 115), dbp=(50, 72))
    return dict(
        cc=(f"黑便 {c.i(1, 5)} 天，每日 {c.i(1, 3)} 次，"
            f"{c.pick(['今日呕血 1 次，量约 ', '今晨呕吐咖啡样物 1 次，量约 '])}{c.n(100, 400, 'mL')}，伴头晕、乏力"),
        hist=[f"胃溃疡病史 {c.i(2, 15)} 年", "长期口服阿司匹林，剂量未提供",
              f"饮酒 {c.i(5, 30)} 年，每日约 {c.i(1, 5)} 两白酒", "否认肝病史"],
        hist_miss=["既往胃镜结果", "抗凝药使用史", "幽门螺杆菌检测史"],
        vit=vit, signs=["面色及睑结膜苍白"], neg=["腹部无明显压痛", "肝脾肋下未触及", "无肝掌及蜘蛛痣"],
        labs=[c.lab("血红蛋白", 62, 102, "g/L", 0, LOW), c.lab("血小板", 130, 300, "×10^9/L"),
              c.lab("尿素氮", 8, 19, "mmol/L", 1)],
        labs_miss=["胃镜", "凝血功能", "既往血红蛋白", "血型"],
        imp=c.pick(["上消化道出血，消化性溃疡出血可能", "上消化道出血，病因待查"]),
        outside=["院外口服奥美拉唑 1 次，剂量未提供"], conflict=["血红蛋白"])


@scenario("heart_failure", "心衰合并房颤：多问题并存", (60, 90))
def heart_failure(c):
    vit = c.vitals(hr=(98, 142), rr=(22, 30), spo2=(86, 94), sbp=(105, 165), dbp=(60, 95), irregular=True)
    return dict(
        cc=f"活动后气促 {c.i(2, 24)} 个月，加重伴夜间不能平卧 {c.i(2, 7)} 天，伴双下肢水肿",
        hist=[f"高血压 {c.i(5, 25)} 年", f"心房颤动病史 {c.i(1, 10)} 年，抗凝药使用情况未提供",
              f"冠心病病史 {c.i(2, 15)} 年", f"2 型糖尿病 {c.i(2, 15)} 年"],
        hist_miss=["既往超声心动图", "利尿剂使用情况", "饮水及钠盐摄入情况"],
        vit=vit, signs=["颈静脉充盈", "双肺底可闻及湿啰音", "双下肢凹陷性水肿"], neg=["腹部无压痛", "未闻及明显病理性杂音"],
        labs=[c.lab("NT-proBNP", 1800, 14000, "pg/mL", 0, HIGH), c.lab("肌酐", 75, 190, "μmol/L"),
              c.lab("血钾", 3.3, 5.4, "mmol/L", 1)],
        labs_miss=["超声心动图（左室射血分数）", "甲状腺功能", "肌钙蛋白", "既往 NT-proBNP"],
        extra=[f"心电图：心房颤动，心室率约 {c.hr} 次/分"],
        imp="慢性心力衰竭急性加重，心房颤动",
        outside=["院外口服呋塞米 20 mg，每日 1 次，已 3 天，尿量变化未提供"], conflict=["NT-proBNP"])


@scenario("anemia_renal", "贫血伴肾功能异常：病因待查（同赛题示例）", (40, 82))
def anemia_renal(c):
    vit = c.vitals(sbp=(120, 170), hr=(85, 108))
    return dict(
        cc=f"乏力 {c.i(1, 8)} 周，活动后心悸；现有记录未见明确黑便、肉眼血尿或其他出血表现",
        hist=[f"高血压 {c.i(3, 20)} 年", f"2 型糖尿病 {c.i(3, 20)} 年", "否认肾脏病史"],
        hist_miss=["药物史", "肾功能基线", "既往贫血检查"],
        vit=vit, signs=["结膜苍白", c.pick(["双下肢轻度水肿", "双下肢无水肿"])], neg=["心肺听诊未见明显异常", "皮肤未见出血点"],
        labs=[c.lab("血红蛋白", 65, 98, "g/L", 0, LOW), c.lab("肌酐", 150, 420, "μmol/L", 0, HIGH),
              c.lab("血钾", 3.8, 5.6, "mmol/L", 1)],
        labs_miss=["红细胞指数", "网织红细胞", "尿常规", "铁代谢", "既往肌酐"],
        imp="贫血及肾功能异常，原因待查", conflict=["血红蛋白", "肌酐"])


@scenario("copd", "慢阻肺急性加重：血气解读、呼吸衰竭待排", (55, 86))
def copd(c):
    vit = c.vitals(t=(36.5, 38.3), hr=(92, 120), rr=(24, 32), spo2=(82, 91))
    return dict(
        cc=f"反复咳嗽、咳痰 {c.i(5, 25)} 年，气促加重 {c.i(2, 7)} 天，痰量增多、呈黄色",
        hist=[f"吸烟 {c.i(20, 45)} 年，每日约 {c.pick([10, 20, 30])} 支",
              f"慢性阻塞性肺疾病病史 {c.i(3, 15)} 年，规律吸入药物情况未提供", f"高血压 {c.i(3, 15)} 年"],
        hist_miss=["肺功能结果", "家庭氧疗情况", "近一年急性加重次数"],
        vit=vit, signs=["桶状胸", "双肺呼吸音减低，可闻及散在哮鸣音", c.pick(["口唇轻度发绀", "口唇无发绀"])],
        neg=["双下肢无水肿", "无明显意识障碍"],
        labs=[c.lab("动脉血氧分压", 48, 65, "mmHg"), c.lab("动脉血二氧化碳分压", 46, 72, "mmHg"), wbc(c, 7, 15)],
        labs_miss=["痰培养", "胸部 CT", "既往血气分析", "血气采样时吸氧条件"],
        extra=["胸部 X 线：双肺纹理增多，肺透亮度增加"],
        imp=c.pick(["慢性阻塞性肺疾病急性加重", "慢性阻塞性肺疾病急性加重，Ⅱ型呼吸衰竭待排"]),
        outside=["院外自行吸入沙丁胺醇，症状缓解不明显", "院外口服头孢克洛 3 天，剂量未提供"],
        conflict=["动脉血二氧化碳分压"])


@scenario("pancreatitis", "急性胰腺炎：病因待查、严重度评估资料不足", (25, 72))
def pancreatitis(c):
    vit = c.vitals(t=(36.8, 38.5), hr=(92, 120))
    return dict(
        cc=(f"上腹部持续性疼痛 {c.i(6, 48)} 小时，向腰背部放射，伴恶心、呕吐，"
            f"发病前{c.pick(['有饮酒', '进食油腻食物'])}"),
        hist=[f"胆囊结石病史 {c.i(1, 10)} 年", f"饮酒 {c.i(5, 25)} 年", "高脂血症病史，用药情况未提供", "否认腹部手术史"],
        hist_miss=["既往胰腺炎发作史", "用药史"],
        vit=vit, signs=["上腹部压痛"], neg=["无明显反跳痛", "墨菲征阴性"],
        labs=[c.lab("血淀粉酶", 320, 1800, "U/L", 0, HIGH), c.lab("脂肪酶", 450, 3200, "U/L"),
              c.lab("血钙", 1.9, 2.35, "mmol/L", 2), wbc(c, 10, 18)],
        labs_miss=["血脂", "腹部 CT", "肝功能", "动脉血气"],
        extra=[c.pick(["腹部超声：胰腺显示欠清，胆囊多发结石", "腹部超声：胰腺体积增大，胰周少量积液"])],
        imp="急性胰腺炎可能，病因待查", conflict=["血淀粉酶"])


@scenario("pyelonephritis", "尿路感染：上下尿路鉴别、院外抗生素", (20, 78))
def pyelonephritis(c):
    side = c.pick(["右", "左"])
    vit = c.vitals(t=(37.8, 39.6), hr=(90, 118))
    return dict(
        cc=(f"尿频、尿急、尿痛 {c.i(2, 6)} 天，发热伴{side}侧腰痛 {c.i(1, 3)} 天，"
            f"最高体温 {c.n(38.3, 40, '℃', 1)}"),
        hist=[f"2 型糖尿病 {c.i(2, 15)} 年", "泌尿系结石病史", "既往体健"],
        hist_miss=["近期导尿或泌尿系操作史", "既往尿路感染次数"], preg=True,
        vit=vit, signs=[f"{side}肾区叩击痛"], neg=["腹部无压痛、反跳痛", "输尿管走行区无压痛"],
        labs=[wbc(c, 11, 20), c.lab("C 反应蛋白", 40, 200, "mg/L"), c.lab("肌酐", 55, 115, "μmol/L"),
              "尿常规：白细胞 3+，亚硝酸盐阳性"],
        labs_miss=["尿培养", "血培养", "泌尿系超声"],
        imp=f"急性肾盂肾炎可能（{side}侧）",
        outside=["院外口服左氧氟沙星 2 天，剂量未提供"], conflict=["白细胞"])


@scenario("cellulitis", "皮肤软组织感染：范围描述、深部感染待排", (30, 82))
def cellulitis(c):
    side = c.pick(["右", "左"])
    vit = c.vitals(t=(37.5, 39.2), hr=(85, 112))
    return dict(
        cc=f"{side}小腿红肿、疼痛 {c.i(2, 6)} 天，伴发热，最高体温 {c.n(37.8, 39.5, '℃', 1)}",
        hist=[f"2 型糖尿病 {c.i(2, 15)} 年", "足癣病史", "下肢静脉曲张病史", "否认外伤史"],
        hist_miss=["血糖控制情况", "既往类似发作史"],
        vit=vit,
        signs=[f"{side}小腿前侧皮肤红肿，范围约 {c.i(6, 16)} cm × {c.i(4, 10)} cm，皮温升高，压痛明显，边界不清"],
        neg=["未触及波动感", "足背动脉搏动可触及"],
        labs=[wbc(c, 10, 17), c.lab("C 反应蛋白", 20, 150, "mg/L"), c.lab("血糖", 5.5, 16, "mmol/L", 1)],
        labs_miss=["下肢血管超声", "血培养", "局部分泌物培养"],
        imp=f"{side}下肢蜂窝织炎可能",
        outside=["院外口服头孢呋辛 2 天，剂量未提供"], conflict=["C 反应蛋白"])


@scenario("pe", "胸闷气促：肺栓塞可能、危险因素", (30, 82))
def pe(c):
    vit = c.vitals(hr=(100, 128), rr=(22, 30), spo2=(85, 93))
    return dict(
        cc=f"突发胸闷、气促 {c.i(2, 24)} 小时，伴{c.pick(['右侧胸痛', '心悸', '咳嗽'])}",
        hist=[[f"{c.i(5, 14)} 天前行{c.pick(['膝关节置换术', '髋关节置换术', '腹部手术'])}",
               f"近期长途乘车约 {c.i(8, 20)} 小时", "近期无手术及制动史"],
              f"高血压 {c.i(2, 15)} 年", "否认静脉血栓病史"],
        hist_miss=["肿瘤病史", "激素类药物使用史", "家族血栓史"],
        vit=vit, signs=[c.pick(["左下肢较右下肢肿胀", "双下肢无明显不对称肿胀"])],
        neg=["双肺呼吸音清，未闻及干湿啰音", "心律齐"],
        labs=[c.lab("D-二聚体", 1.2, 8.0, "mg/L", 2, "（实验室参考上限 0.5 mg/L）"),
              c.lab("肌钙蛋白 I", 0.01, 0.3, "ng/mL", 2)],
        labs_miss=["CT 肺动脉造影", "下肢静脉超声", "动脉血气", "超声心动图"],
        extra=[f"心电图：窦性心动过速，心率 {c.hr} 次/分"],
        imp="胸闷气促待查，肺栓塞可能", conflict=["D-二聚体"])


@scenario("cirrhosis", "肝硬化失代偿：多项异常、并发症待排", (40, 78))
def cirrhosis(c):
    vit = c.vitals(t=(36.3, 37.6), hr=(75, 100), sbp=(95, 130))
    return dict(
        cc=f"腹胀、双下肢水肿 {c.i(1, 6)} 周，尿量减少" + c.pick(["", "，近 2 天出现反应迟钝"]),
        hist=[[f"乙型肝炎病史 {c.i(5, 25)} 年，抗病毒治疗情况未提供",
               f"饮酒 {c.i(10, 30)} 年，每日约 {c.i(2, 6)} 两白酒"],
              f"肝硬化病史 {c.i(1, 8)} 年", "否认消化道出血史"],
        hist_miss=["既往胃镜结果", "利尿剂使用情况"],
        vit=vit, signs=["腹部膨隆，移动性浊音阳性", "肝掌", "双下肢凹陷性水肿", c.pick(["巩膜轻度黄染", "巩膜无黄染"])],
        neg=["无扑翼样震颤", "腹部无明显压痛"],
        labs=[c.lab("白蛋白", 21, 32, "g/L", 0, LOW), c.lab("总胆红素", 25, 90, "μmol/L"),
              c.lab("血小板", 35, 90, "×10^9/L"), c.lab("凝血酶原时间", 13.5, 19.5, "s", 1)],
        labs_miss=["腹水常规", "甲胎蛋白", "血氨", "胃镜"],
        extra=["腹部超声：肝脏体积缩小、表面不光滑，脾大，腹腔大量积液"],
        imp=c.pick(["肝硬化失代偿期，腹腔积液", "肝硬化失代偿期，腹腔积液，腹腔感染待排"]),
        outside=["院外口服螺内酯及呋塞米，剂量未提供"], conflict=["白蛋白"])


@scenario("aki", "急性肾损伤伴高钾：危急值与诱因", (50, 88))
def aki(c):
    vit = c.vitals(hr=(88, 115), sbp=(90, 125), dbp=(50, 75))
    return dict(
        cc=f"腹泻 {c.i(2, 6)} 天，每日 {c.i(4, 10)} 次水样便，伴乏力、尿量明显减少",
        hist=[f"高血压 {c.i(3, 20)} 年，口服厄贝沙坦，剂量未提供", f"2 型糖尿病 {c.i(2, 15)} 年",
              "近期自服布洛芬止痛，剂量未提供", "否认肾脏病史"],
        hist_miss=["基线肌酐", "每日尿量具体数值"],
        vit=vit, signs=["皮肤弹性差，口唇干燥"], neg=["双下肢无水肿", "腹部无明显压痛"],
        labs=[c.lab("肌酐", 230, 620, "μmol/L", 0, HIGH), c.lab("血钾", 5.6, 7.0, "mmol/L", 1, HIGH),
              c.lab("碳酸氢根", 12, 20, "mmol/L", 1), c.lab("尿素氮", 14, 36, "mmol/L", 1)],
        labs_miss=["泌尿系超声", "尿常规", "既往肌酐", "粪便常规"],
        extra=[c.pick(["心电图：T 波高尖", "心电图：窦性心律，未见明显 T 波高尖"])],
        imp="急性肾损伤，高钾血症", conflict=["血钾", "肌酐"])


@scenario("asthma", "哮喘急性发作：院外已用药、严重度", (15, 60))
def asthma(c):
    vit = c.vitals(hr=(95, 125), rr=(22, 30), spo2=(88, 95))
    return dict(
        cc=(f"反复发作性喘息 {c.i(2, 20)} 年，{c.pick(['接触冷空气后', '感冒后', '打扫卫生后'])}"
            f"再发加重 {c.i(1, 3)} 天"),
        hist=["支气管哮喘病史，规律用药情况未提供", "过敏性鼻炎病史", "否认吸烟"],
        hist_miss=["既往急性发作及住院次数", "过敏原检测结果"],
        vit=vit, signs=["双肺可闻及广泛哮鸣音，呼气相延长"], neg=["无明显发绀", "能说完整句子"],
        labs=[c.lab("动脉血氧分压", 58, 80, "mmHg"), c.lab("动脉血二氧化碳分压", 32, 46, "mmHg"), wbc(c, 6, 12)],
        labs_miss=["肺功能", "呼气峰流速", "胸部影像"],
        imp="支气管哮喘急性发作",
        outside=[f"院外已吸入沙丁胺醇 {c.i(2, 6)} 次，症状缓解不明显"], conflict=["动脉血二氧化碳分压"])


@scenario("cholecystitis", "胆囊炎：典型体征、胆管受累待排", (30, 78))
def cholecystitis(c):
    vit = c.vitals(t=(37.4, 38.8), hr=(85, 112))
    return dict(
        cc=(f"右上腹疼痛 {c.i(6, 48)} 小时，进食油腻后加重，向右肩背部放射，伴发热，"
            f"最高体温 {c.n(37.8, 39, '℃', 1)}"),
        hist=[f"胆囊结石病史 {c.i(1, 10)} 年", f"2 型糖尿病 {c.i(2, 15)} 年", "否认腹部手术史"],
        hist_miss=["既往类似发作史", "用药史"], preg=True,
        vit=vit, signs=["右上腹压痛，墨菲征阳性"], neg=["无反跳痛", "皮肤巩膜无黄染"],
        labs=[wbc(c, 10.5, 17), c.lab("C 反应蛋白", 20, 160, "mg/L"), c.lab("总胆红素", 8, 30, "μmol/L", 1),
              c.lab("丙氨酸氨基转移酶", 18, 120, "U/L")],
        labs_miss=["血淀粉酶", "磁共振胰胆管成像", "凝血功能"],
        extra=[f"腹部超声：胆囊增大，壁增厚约 {c.n(4, 8, 'mm')}，胆囊颈部结石嵌顿"],
        imp="急性结石性胆囊炎可能",
        outside=["院外口服消炎利胆片，剂量未提供"], conflict=["白细胞"])


@scenario("syncope", "晕厥：心源性与神经介导鉴别、资料缺口", (40, 86))
def syncope(c):
    vit = c.vitals(hr=(45, 95))
    ecg = "窦性心动过缓" if c.hr < 60 else "窦性心律"
    return dict(
        cc=(f"{c.pick(['如厕时', '起立时', '活动中'])}突发意识丧失 1 次，持续约 {c.i(1, 3)} 分钟后自行恢复，"
            f"{c.pick(['无抽搐、无尿失禁', '发作时有短暂肢体抖动'])}，醒后无肢体无力"),
        hist=[f"高血压 {c.i(3, 20)} 年，口服降压药，名称未提供", "冠心病病史", "否认癫痫病史", f"2 型糖尿病 {c.i(2, 15)} 年"],
        hist_miss=["既往晕厥史", "家族猝死史"],
        vit=vit, signs=["神志清楚，对答切题"], neg=["神经系统查体未见定位体征", "心脏各瓣膜区未闻及杂音"],
        labs=[c.lab("血糖", 4.5, 9, "mmol/L", 1), c.lab("血红蛋白", 110, 150, "g/L"), c.lab("血钾", 3.3, 4.8, "mmol/L", 1)],
        labs_miss=["动态心电图", "超声心动图", "立卧位血压", "目击者描述"],
        extra=[c.pick([f"心电图：{ecg}，心率 {c.hr} 次/分", "心电图：二度Ⅱ型房室传导阻滞"])],
        imp="晕厥待查")


@scenario("fuo", "发热待查：信息不足、避免过早下结论", (20, 72))
def fuo(c):
    vit = c.vitals(t=(37.6, 39.2), hr=(85, 110))
    return dict(
        cc=(f"反复发热 {c.i(3, 8)} 周，最高体温 {c.n(38.5, 40, '℃', 1)}，多于午后出现，"
            f"伴盗汗、体重下降约 {c.n(2, 8, 'kg')}"),
        hist=["既往体健", f"2 型糖尿病 {c.i(2, 15)} 年", "否认结核接触史"],
        hist_miss=["旅行史", "动物接触史", "近期用药史"],
        vit=vit, signs=[c.pick([f"右颈部可触及数枚肿大淋巴结，最大约 {c.n(1, 2.5, 'cm', 1)}，质韧、无压痛",
                                "未触及浅表淋巴结肿大"])],
        neg=["心肺听诊未见明显异常", "肝脾肋下未触及"],
        labs=[wbc(c, 3.0, 12), c.lab("血红蛋白", 92, 130, "g/L"), c.lab("C 反应蛋白", 15, 120, "mg/L"),
              c.lab("红细胞沉降率", 30, 95, "mm/h")],
        labs_miss=["血培养", "结核相关检查", "HIV 抗体", "自身抗体", "胸部 CT"],
        extra=["胸部 X 线：未见明显异常"], imp="发热待查",
        outside=["院外先后口服多种抗生素，名称及疗程未提供"], conflict=["C 反应蛋白"])


@scenario("hypertensive", "血压显著升高：靶器官评估、停药诱因", (35, 82))
def hypertensive(c):
    vit = c.vitals(sbp=(186, 232), dbp=(110, 135), hr=(70, 100))
    return dict(
        cc=f"头痛、头晕 {c.i(3, 24)} 小时，伴{c.pick(['视物模糊', '恶心', '胸闷'])}",
        hist=[f"高血压 {c.i(5, 20)} 年，近 {c.i(3, 10)} 天自行停药", f"2 型糖尿病 {c.i(2, 15)} 年", f"吸烟 {c.i(10, 30)} 年"],
        hist_miss=["降压药名称及剂量", "既往血压控制情况"],
        vit=vit, signs=["神志清楚"], neg=["神经系统查体未见定位体征", "双肺呼吸音清", "双下肢无水肿"],
        labs=[c.lab("肌酐", 70, 160, "μmol/L"), c.lab("血钾", 3.3, 4.8, "mmol/L", 1),
              c.lab("肌钙蛋白 I", 0.01, 0.03, "ng/mL", 2)],
        labs_miss=["眼底检查", "尿常规", "超声心动图"],
        extra=["头颅 CT：未见颅内出血"], imp="高血压急症可能，靶器官损害待评估",
        outside=["院外自行口服硝苯地平 1 片，血压下降情况未提供"])


# ---------------- 组装 ----------------

ALLERGY = ["青霉素过敏史，具体反应未提供", "对磺胺类药物过敏（皮疹）", "对头孢类药物过敏，具体药物未提供"]


def build_case(r, sc, cid):
    c = Case(r)
    if sc["sex"]:
        c.sex = sc["sex"]
    c.age = c.i(*sc["age"])
    p = sc["build"](c)
    srcs, outside = [], None

    if r.random() < 0.04:
        demo = "成人"
        c.traps.append("人口学信息不全")
        demo_missing = ["性别与年龄"]
    else:
        demo = f"{c.sex}性，{c.age} 岁"
        demo_missing = []
    s1 = f"{demo}，{p['cc']}"
    if p.get("time_conflict") and r.random() < 0.35:
        s1 += "；" + c.pick(p["time_conflict"])
        c.traps.append("时序冲突")
    srcs.append(s1)

    if r.random() < 0.08:  # 资料极少：只有主诉与初步判断
        srcs.append("既往史、体格检查及辅助检查资料均未提供")
        c.missing = demo_missing + ["既往史", "体格检查", "辅助检查"]
        c.traps = [t for t in c.traps if t != "生命体征缺项"] + ["资料极少"]
    else:
        c.missing = demo_missing + c.missing
        hist = [c.pick(h) if isinstance(h, list) else h for h in c.some(p["hist"], 1, 3)]
        if r.random() < 0.15:
            hist.append(c.pick(ALLERGY))
            c.traps.append("过敏史")
        hm = c.some(p["hist_miss"], 1, 2)
        if p.get("preg") and c.sex == "女" and 15 <= c.age <= 50:
            hm.append("末次月经及妊娠情况")
        c.missing += hm
        srcs.append("既往史：" + "；".join(hist) + (f"；{'、'.join(hm)}未提供" if hm else ""))
        srcs.append("查体：" + p["vit"] + "；" + "，".join(p["signs"] + c.some(p["neg"], 1, 2)))
        lm = c.some(p["labs_miss"], 1, 3)
        c.missing += lm
        srcs.append("辅助检查：" + "；".join(p["labs"]) + (f"；未提供{'、'.join(lm)}" if lm else ""))
        if p.get("extra"):
            srcs.append("；".join(p["extra"]))
        if p.get("conflict") and r.random() < 0.18:
            name = c.pick(p["conflict"])
            v, u, d = c.labs[name]
            v2 = max(v * (1 + c.pick([-1, 1]) * r.uniform(0.25, 0.45)), 0.01)
            s = c.unit(c.fmt(v2, d), u)
            c.numbers.append(s)
            srcs.append(f"另一份急诊检验单记录{name} {s}，与上述结果不一致，采样时间及原因未说明")
            c.traps.append("检验结果冲突")
        if p.get("outside") and r.random() < 0.3:
            outside = c.pick(p["outside"])
            srcs.append("院外诊治：" + outside)
            c.traps.append("院外已实施治疗")
    srcs.append("入院初步判断：" + p["imp"])

    text = "\n".join(srcs)
    case = {"case_id": cid, "record_cutoff": "T0",
            "sources": [{"source_id": f"S{k + 1:02d}", "content": t} for k, t in enumerate(srcs)]}
    meta = {"case_id": cid, "scenario": sc["name"], "focus": sc["focus"], "traps": c.traps,
            "missing_items": c.missing,
            "key_numbers": [x for x in dict.fromkeys(c.numbers) if x in text],
            "admission_impression": p["imp"], "outside_treatment": outside}
    return case, meta


def generate(n, seed):
    r = random.Random(seed)
    order = [SCEN[k % len(SCEN)] for k in range(n)]
    r.shuffle(order)
    cases, metas = [], []
    for k, sc in enumerate(order):
        case, meta = build_case(r, sc, f"T{k + 1:04d}")
        cases.append(case)
        metas.append(meta)
    return cases, metas


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=500)
    ap.add_argument("--seed", type=int, default=2026)
    ap.add_argument("--out", default=None, help="默认 data/test_cases_<n>.json")
    a = ap.parse_args()
    out = Path(a.out) if a.out else ROOT / "data" / f"test_cases_{a.n}.json"
    cases, metas = generate(a.n, a.seed)
    out.write_text(json.dumps(cases, ensure_ascii=False, indent=2), encoding="utf-8")
    note = ("合成教学病例：由 tools/gen_test_cases.py 按模板与随机参数生成（seed=%d），不来自真实患者，"
            "医学合理性需指导教师抽查。" % a.seed)
    meta_path = out.with_name(out.stem + "_meta.json")
    meta_path.write_text(json.dumps({"note": note, "seed": a.seed, "cases": metas}, ensure_ascii=False, indent=2),
                         encoding="utf-8")
    print(f"已生成 {len(cases)} 例 -> {out}")
    print(f"元数据 -> {meta_path}")
