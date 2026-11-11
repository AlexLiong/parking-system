"""Exact current-visit lookup and location guidance (FR-10.4)."""
from ..core.errors import NotFound,StateConflict,ValidationError
from ..core.timeutil import iso_local
from ..lpr.charset import normalize_valid_plate

def find_car(container, value):
    try:plate,_=normalize_valid_plate(value)
    except ValueError as exc:raise ValidationError(f'车牌格式无效：{exc}') from exc
    with container.db.scope():
        record=container.parking_repo.find_open_by_plate(plate)
        if record is None:raise NotFound('未找到该车牌的在场记录')
        result={'plate':plate,'record_id':record.id,'entry_time':iso_local(record.entry_time),
                'entry_photo':record.entry_photo,'entry_plate_crop':record.entry_plate_crop,
                'space_no':record.space_no,'located':False,'location':None,'navigation':[]}
        if not record.space_no:
            result['message']='该记录未分配车位，请到岗亭核实停车位置';return result
        space=container.space_repo.get(record.space_no)
        if space is None or space.disabled or space.current_plate!=plate or space.status!='occupied':
            raise StateConflict('location_inconsistent','车位状态与在场记录不一致，请联系管理员核实')
        row='北侧' if space.z>0 else '南侧'
        side='东侧' if space.x>0 else '西侧'
        result.update(located=True,location={'zone':space.zone,'x':space.x,'z':space.z,'rotation':space.rotation},
                      navigation=['从入口进入场内',f'前往{row} {space.zone}区{side}',f'找到 {space.space_no} 车位，对照入场照片确认车辆'])
        return result
