"""Conformal swept-prism thermal mesh using a Gmsh planar arrangement.

All material outlines are imprinted on one triangulation. Shared node indices
across layers make copper/laminate/via/terminal interfaces conformal without
tetrahedralizing 18 um copper at the board's lateral length scale.
"""
import json
import math
from collections import defaultdict
from pathlib import Path
import numpy as np
from .common import WorkflowError, write_json

def parts(shape, kind='Polygon'):
    if shape.is_empty:return []
    if shape.geom_type==kind:return [shape]
    return [p for g in getattr(shape,'geoms',[]) for p in parts(g,kind)]


def stack_levels(stack, merge_plies=False):
    """Optionally coarsen numerical interfaces between identical FR4 subplies.

    Saved stackup entries are untouched. Copper/mask interfaces always remain;
    only adjacent dielectric subplies bearing the same saved layer name merge.
    This is a mesh-coarsening option that requires comparison against the full
    ply mesh, since temperatures can vary nonlinearly through a dielectric.
    """
    if not isinstance(merge_plies,bool):
        raise WorkflowError('mesh.merge_identical_dielectric_plies must be boolean')
    levels={round(float(layer[key]),9) for layer in stack for key in ('z_top_mm','z_bottom_mm')}
    removed=[]
    if merge_plies:
        ordered=sorted(stack,key=lambda layer:layer['z_bottom_mm'])
        for below,above in zip(ordered,ordered[1:]):
            shared=round(float(below['z_top_mm']),9)
            if (below['kind']==above['kind']=='dielectric' and below['name']==above['name']
                    and shared==round(float(above['z_bottom_mm']),9)):
                levels.remove(shared);removed.append({'layer':below['name'],'z_mm':shared})
    return levels,removed

def layer_at_height(stack, height):
    # A merged interval may have its midpoint exactly on a saved subply edge.
    # Half-open intervals assign that point to the upper layer instead of void.
    return next((layer for layer in stack
                 if layer['z_bottom_mm'] <= height < layer['z_top_mm']),None)

def prepare(board,case,project=None):
    import shapely
    from shapely.geometry import Polygon,Point,LineString,box
    from shapely.affinity import rotate,translate
    from shapely.ops import unary_union
    def clean(g):return shapely.set_precision(shapely.make_valid(g),1e-6)
    outline=clean(unary_union([Polygon(p['outer'],p['holes']) for p in board['outline']]))
    cfg=case['geometry']; mode=cfg['mode']
    region=outline.intersection(box(*cfg['crop_mm'])) if mode=='crop' else outline
    if region.is_empty:raise WorkflowError('Crop misses the PCB')
    components={x['ref']:x for x in board['components']}
    refs=case['sources']['refs'];resistors={};pad_shapes={'F.Cu':[],'B.Cu':[]}
    copper=defaultdict(list)
    for item in board['copper']:
        for p in item['polygons']:
            g=clean(Polygon(p['outer'],p['holes'])).intersection(region)
            copper[item['layer']].append(g)
            if item['kind']=='pad' and item['layer'] in pad_shapes:pad_shapes[item['layer']].append(g)
    copper={k:clean(unary_union(v)) for k,v in copper.items()}
    if 'soldermask_openings' not in board:
        raise WorkflowError('Extraction lacks actual soldermask openings; rerun extract with the current implementation')
    pads={side:clean(unary_union([Polygon(p['outer'],p['holes']) for p in board['soldermask_openings'][name]])).intersection(region)
          for side,name in [('F.Cu','F.Mask'),('B.Cu','B.Mask')]}
    for ref in refs:
        c=components[ref]
        if c['flipped']:raise WorkflowError('Back-side heat-source bodies require a reflected package model; not supported by this package template')
        points={p['number']:p['position'] for p in c['pads']}
        a,b=np.array(points['1']),np.array(points['2']);angle=math.degrees(math.atan2(*(b-a)[::-1]))
        x,y=c['position'];l,w,height=cfg['resistor_body_mm'];tw=cfg['terminal_width_mm']
        def place(g):return clean(translate(rotate(g,angle,origin=(0,0)),x,y))
        body=place(box(-l/2,-w/2,l/2,w/2))
        if not region.covers(body):raise WorkflowError(f'Crop cuts heat source {ref}')
        ceramic=place(box(-l/2+tw,-w/2,l/2-tw,w/2))
        terminals=body.difference(ceramic)
        solder_parts=[]
        for item in board['copper']:
            if item.get('ref')==ref and item['layer']=='F.Cu':
                solder_parts.extend(Polygon(p['outer'],p['holes']) for p in item['polygons'])
        solder=clean(unary_union(solder_parts)).intersection(terminals)
        if solder.is_empty:raise WorkflowError(f'No solder contact area for {ref}')
        resistors[ref]={'body':body,'ceramic':ceramic,'terminals':terminals,'solder':solder,'height':height}
    detail=region if mode!='hybrid' else clean(unary_union([r['body'].buffer(cfg['detail_margin_mm'],join_style=2) for r in resistors.values()])).intersection(region)
    holes=[];barrels=[];plating=float(cfg.get('plating_mm',0.025))
    for h in board['holes']:
        a,b=h['drill'];radius=min(a,b)/2
        if radius<=0:continue
        x,y=h['center'];half=abs(a-b)/2
        if half:
            line=LineString([(-half,0),(half,0)]) if a>b else LineString([(0,-half),(0,half)])
            hole=translate(rotate(line.buffer(radius,quad_segs=8),-h['angle_deg'],origin=(0,0)),x,y)
        else:hole=Point(x,y).buffer(radius,quad_segs=8)
        hole=clean(hole)
        if not region.intersects(hole):continue
        if not region.covers(hole):raise WorkflowError('Crop intersects a drill; move its edge beyond the complete hole')
        holes.append(hole)
        if h['plated']:
            layers=[l['name'] for l in board['stackup'] if l['kind']=='copper']
            if set(h['layers'])!=set(layers):raise WorkflowError('Blind/buried vias need depth-resolved plating; only through barrels are supported')
            barrels.append(clean(hole.buffer(plating,join_style=2).difference(hole)))
    void=clean(unary_union(holes));barrels=clean(unary_union(barrels));region=clean(region.difference(void))
    copper={k:clean(v.difference(void)) for k,v in copper.items()}
    tiles=[]
    if mode=='hybrid':
        size=cfg['tile_mm'];xmin,ymin,xmax,ymax=region.bounds
        for x in np.arange(math.floor(xmin/size)*size,xmax,size):
            for y in np.arange(math.floor(ymin/size)*size,ymax,size):
                shape=clean(box(x,y,x+size,y+size).intersection(region).difference(detail))
                if shape.area>1e-8:tiles.append(shape)
    from .mechanical import load_parts
    mechanicals=load_parts(case,project) if case.get('mechanicals') else []
    mask_top=max(l['z_top_mm'] for l in board['stackup'])
    for part in mechanicals:
        contact_bottom=part['interface'][0] if part['interface'] else part['z_bottom_mm']
        if abs(contact_bottom-mask_top)>1e-7:
            raise WorkflowError('Mechanical contact must meet the top soldermask plane; adjust STEP placement/contact thickness')
        shape=part['shape']
        if not region.covers(shape):raise WorkflowError('Mechanical contact must be fully supported inside the PCB domain')
        if shape.intersects(pads['F.Cu']) or any(shape.intersects(r['body']) for r in resistors.values()):
            raise WorkflowError('Mechanical contact overlaps a soldermask opening or resistor; supply a supported contact footprint')
        for other in mechanicals:
            if other is not part and shape.intersection(other['shape']).area>1e-8:
                raise WorkflowError('Overlapping mechanical parts are not supported')
    return {'outline':outline,'region':region,'detail':detail,'copper':copper,'pads':pads,
            'barrels':barrels,'resistors':resistors,'tiles':tiles,'mechanicals':mechanicals}

def homogenize(board,case,shape,geo):
    m=case['materials'];lam=m['fr4'];cu=m['copper'];thickness=sum(l['thickness_mm'] for l in board['stackup'])
    ksum=np.zeros(2); resistance=0.;mass=0.;capacity=0.
    via=min(1.,geo['barrels'].intersection(shape).area/shape.area)
    for layer in board['stackup']:
        t=layer['thickness_mm'];base=m['soldermask'] if layer['name'].endswith('.Mask') else lam
        f=geo['copper'].get(layer['name'],shape.difference(shape)).intersection(shape).area/shape.area if layer['kind']=='copper' else 0.
        f=min(1.,max(via,f));k=f*np.array(cu['k'])+(1-f)*np.array(base['k'])
        # The copper fraction is parallel in-plane; layer gaps are in series.
        ksum+=t*k[:2];resistance+=t/k[2]
        rho=f*cu['density']+(1-f)*base['density']
        cap=f*cu['density']*cu['heat_capacity']+(1-f)*base['density']*base['heat_capacity']
        mass+=t*rho;capacity+=t*cap
    return {'k':[float(ksum[0]/thickness),float(ksum[1]/thickness),thickness/resistance],
            'density':mass/thickness,'heat_capacity':capacity/mass,'assumption':'Voigt in-plane mixing; series layer resistance; plated-via area included'}

def triangulate(geo,case,out):
    import gmsh
    import shapely
    from shapely.ops import unary_union,polygonize
    from shapely.geometry import Point
    region=geo['region'];detail=geo['detail']
    contours=[region.boundary,detail.intersection(region).boundary]
    contours.extend(x.intersection(detail).boundary for x in geo['copper'].values())
    contours.extend(x.boundary for x in geo['pads'].values())
    contours.append(geo['barrels'].intersection(detail).boundary)
    contours.extend(x.boundary for x in geo['tiles'])
    for r in geo['resistors'].values():contours.extend(r[k].boundary for k in ('body','ceramic','solder'))
    contours.extend(p['shape'].boundary for p in geo['mechanicals'])
    print('Imprinting planar material interfaces',flush=True)
    # Snap overlay intersections to 0.1 um, far below the extraction chord error.
    # This removes numerical zero-width slivers from overlapping KiCad polygons.
    arrangement=shapely.unary_union(contours,grid_size=float(case['mesh'].get('overlay_grid_mm',1e-4)))
    cells=[p for p in polygonize(arrangement) if p.area>1e-12 and region.covers(p.representative_point())]
    if not cells:raise WorkflowError('No planar material regions')
    print(f'{len(cells)} planar regions',flush=True)
    gmsh.initialize();gmsh.model.add('thermal-planar')
    mesh=case['mesh'];fine=mesh['fine_mm']*mesh['scale'];coarse=mesh['coarse_mm']*mesh['scale']
    gmsh.option.setNumber('General.Terminal',0)
    gmsh.option.setNumber('Mesh.Algorithm',6)
    gmsh.option.setNumber('Mesh.MeshSizeMin',fine/5)
    gmsh.option.setNumber('Mesh.MeshSizeMax',coarse)
    gmsh.option.setNumber('Mesh.MeshSizeExtendFromBoundary',0)
    gmsh.option.setNumber('Mesh.MeshSizeFromCurvature',0)
    vertices={};lines={}
    def vertex(p):
        key=tuple(round(float(v),6) for v in p[:2])
        if key not in vertices:vertices[key]=gmsh.model.geo.addPoint(*key,0,fine if detail.covers(Point(key)) else coarse)
        return vertices[key]
    def loop(ring):
        ids=[vertex(p) for p in list(ring.coords)[:-1]]; ids=[v for i,v in enumerate(ids) if v!=ids[i-1]]
        edges=[]
        for a,b in zip(ids,ids[1:]+ids[:1]):
            if a==b:continue
            key=tuple(sorted((a,b)))
            if key not in lines:lines[key]=gmsh.model.geo.addLine(*key)
            edges.append(lines[key] if a<b else -lines[key])
        if len(edges)<3:raise WorkflowError('Degenerate planar loop; inspect extraction tolerance')
        return gmsh.model.geo.addCurveLoop(edges)
    try:
        for p in cells:
            gmsh.model.geo.addPlaneSurface([loop(p.exterior)]+[loop(h) for h in p.interiors])
        gmsh.model.geo.synchronize();gmsh.model.mesh.generate(2)
        gmsh.write(str(out/'planar.msh'));gmsh.write(str(out/'planar.geo_unrolled'))
        tags,coords,_=gmsh.model.mesh.getNodes();coords=np.asarray(coords).reshape(-1,3)
        order=np.argsort(tags);tags=tags[order];coords=coords[order,:2]
        _,node_tags=gmsh.model.mesh.getElementsByType(2)
        triangles=np.searchsorted(tags,np.asarray(node_tags).reshape(-1,3))
        # Consistent upward triangle orientation for the wedge node ordering.
        a=coords[triangles[:,1]]-coords[triangles[:,0]];b=coords[triangles[:,2]]-coords[triangles[:,0]]
        neg=a[:,0]*b[:,1]-a[:,1]*b[:,0]<0
        triangles[neg]=triangles[neg][:,[0,2,1]]
        print(f'Gmsh: {len(coords)} planar nodes, {len(triangles)} triangles',flush=True)
        return coords,triangles
    finally:gmsh.finalize()

def mesh(board,case,power,out,project=None):
    import shapely
    out=Path(out);out.mkdir(parents=True,exist_ok=True)
    geo=prepare(board,case,project)
    xy,tri=triangulate(geo,case,out);centres=xy[tri].mean(axis=1);pts=shapely.points(centres)
    def inside(g):
        shapely.prepare(g)
        return shapely.covers(g,pts)
    detailed=inside(geo['detail']);barrels=inside(geo['barrels'])&detailed
    materials=dict(case['materials']); bodies={};body_ids={}
    def body(name,material,ref=None):
        if name not in body_ids:
            i=len(body_ids)+1;body_ids[name]=i;bodies[i]={'name':name,'material':material,'ref':ref,'volume_m3':0.0}
        return body_ids[name]
    tile_id=np.zeros(len(tri),dtype=np.int32)
    for i,shape in enumerate(geo['tiles']):
        key=f'hybrid_{i}';materials[key]=homogenize(board,case,shape,geo)
        tile_id[inside(shape)]=body(key,key)
    stack=board['stackup'];solder_top=case['geometry'].get('solder_mm',0.1)
    levels,merged_plies=stack_levels(stack,case['mesh'].get('merge_identical_dielectric_plies',False))
    z=sorted(levels |
             {solder_top,solder_top+case['geometry']['resistor_body_mm'][2]})
    z=sorted(set(z)|{v for p in geo['mechanicals'] for v in (p['z_bottom_mm'],p['z_top_mm'])})
    mechanical_masks=[inside(p['shape']) for p in geo['mechanicals']]
    masks={name:inside(g) for name,g in geo['pads'].items()}
    copper={name:inside(g) for name,g in geo['copper'].items()}
    resistor_masks={ref:{k:inside(r[k]) for k in ('ceramic','terminals','solder')} for ref,r in geo['resistors'].items()}
    ids=np.zeros((len(z)-1,len(tri)),dtype=np.int32)
    cu_top=max(l['z_top_mm'] for l in stack if l['kind']=='copper');cu_bot=min(l['z_bottom_mm'] for l in stack if l['kind']=='copper')
    for j,(lo,hi) in enumerate(zip(z,z[1:])):
        mid=(lo+hi)/2;row=ids[j]
        layer=layer_at_height(stack,mid)
        if layer:
            mask=layer['name'].endswith('.Mask')
            row[:]=body('board_'+('mask' if mask else 'fr4'),'soldermask' if mask else 'fr4')
            if layer['kind']=='copper':row[copper.get(layer['name'],np.zeros(len(tri),bool))&detailed]=body('board_copper','copper')
            if cu_bot<mid<cu_top:row[barrels]=body('board_copper','copper')
            row[~detailed]=tile_id[~detailed]
            if mask:row[masks['F.Cu' if layer['name']=='F.Mask' else 'B.Cu']]=0
        for ref,r in resistor_masks.items():
            if 0<mid<solder_top:row[r['solder']]=body('solder_'+ref,'solder')
            elif solder_top<mid<solder_top+geo['resistors'][ref]['height']:
                row[r['ceramic']]=body('heater_'+ref,'resistor',ref)
                row[r['terminals']]=body('terminal_'+ref,'terminal')
        for part,footprint in zip(geo['mechanicals'],mechanical_masks):
            if part['z_bottom_mm']<mid<part['z_top_mm']:
                row[footprint]=body('mechanical_'+part['name'],part['material'])
            elif part['interface'] and part['interface'][0]<mid<part['interface'][1]:
                row[footprint]=body('contact_'+part['name'],part['contact']['material'])
    if np.any((ids==0).all(axis=0)):
        # Empty columns are allowed only where the planar board truly has no solid.
        raise WorkflowError('A board column has no material; inspect hybrid coverage')
    n=len(xy); nodes=np.vstack([np.column_stack((xy,np.full(n,height))) for height in z])*1e-3
    a=xy[tri[:,1]]-xy[tri[:,0]];b=xy[tri[:,2]]-xy[tri[:,0]]
    area=np.abs(a[:,0]*b[:,1]-a[:,1]*b[:,0])/2
    elements=[];element_ids=[]
    for j,row in enumerate(ids):
        active=row>0;t=tri[active]
        elements.append(np.hstack((t+j*n,t+(j+1)*n)));element_ids.append(row[active])
        for ident in np.unique(row[active]):bodies[int(ident)]['volume_m3']+=float(area[row==ident].sum()*(z[j+1]-z[j])*1e-9)
    wedges=np.vstack(elements);volume_ids=np.concatenate(element_ids)
    # Find two-dimensional neighboring cells once, then expose only solid/void faces.
    edges=np.vstack((tri[:,[0,1]],tri[:,[1,2]],tri[:,[2,0]]));owners=np.tile(np.arange(len(tri)),3)
    es=np.sort(edges,axis=1);order=np.lexsort((es[:,1],es[:,0]));es=es[order];owners=owners[order]
    starts=np.r_[True,np.any(es[1:]!=es[:-1],axis=1)];indices=np.flatnonzero(starts);counts=np.diff(np.r_[indices,len(es)])
    if counts.max()>2:raise WorkflowError('Nonmanifold planar mesh')
    e=es[indices];left=owners[indices];right=np.where(counts==2,owners[np.minimum(indices+1,len(owners)-1)],-1)
    midpoints=xy[e].mean(axis=1);edge_points=shapely.points(midpoints)
    # The arrangement snaps each coordinate by up to half an overlay-grid step.
    # Match boundaries at that precision; a tighter tolerance loses crop tags.
    boundary_tolerance=max(2e-6,float(case['mesh'].get('overlay_grid_mm',1e-4)))
    artificial=(shapely.distance(geo['region'].boundary,edge_points)<boundary_tolerance)&(shapely.distance(geo['outline'].boundary,edge_points)>boundary_tolerance)
    if case['geometry']['mode']!='crop':artificial[:]=False
    # Drill boundaries are exposed, not artificial cuts.
    crop=case['geometry'].get('crop_mm')
    if crop:
        artificial &= np.minimum.reduce([abs(midpoints[:,0]-crop[0]),abs(midpoints[:,0]-crop[2]),abs(midpoints[:,1]-crop[1]),abs(midpoints[:,1]-crop[3])])<boundary_tolerance
    face_tri=[];face_quad=[];quad_ids=[]
    for j in range(len(z)):
        below=ids[j-1]>0 if j>0 else np.zeros(len(tri),bool)
        above=ids[j]>0 if j<len(ids) else np.zeros(len(tri),bool)
        face_tri.append(tri[below^above]+j*n)
    for j,row in enumerate(ids):
        l=row[left]>0;r=np.where(right>=0,row[np.maximum(right,0)]>0,False);exposed=l^r
        ee=e[exposed];face_quad.append(np.column_stack((ee[:,0]+j*n,ee[:,1]+j*n,ee[:,1]+(j+1)*n,ee[:,0]+(j+1)*n)))
        quad_ids.append(np.where(artificial[exposed],102,101))
    face_tri=np.vstack(face_tri);face_quad=np.vstack(face_quad);quad_ids=np.concatenate(quad_ids)
    used=np.unique(np.concatenate((wedges.ravel(),face_tri.ravel(),face_quad.ravel())))
    lookup=np.full(len(nodes),-1,dtype=np.int64);lookup[used]=np.arange(len(used))
    nodes=nodes[used];wedges=lookup[wedges];face_tri=lookup[face_tri];face_quad=lookup[face_quad]
    # Elmer evaluates the surface metric in metres. Catch vanishing overlay
    # slivers here, before launching an expensive assembly or linear solve.
    pp=nodes[face_tri]
    surface_areas=np.linalg.norm(np.cross(pp[:,1]-pp[:,0],pp[:,2]-pp[:,0]),axis=1)/2
    minimum=float(surface_areas.min())
    quality={'minimum_surface_triangle_area_m2':minimum,
             'surface_triangle_area_quantiles_m2':np.quantile(surface_areas,[0,.001,.01,.5,1]).tolist(),
             'overlay_grid_mm':float(case['mesh'].get('overlay_grid_mm',1e-4)),
             'surface_metric_pass':minimum>3e-16}
    write_json(out/'quality.json',quality)
    if not quality['surface_metric_pass']:
        raise WorkflowError('Surface slivers fall below the Elmer metric tolerance; inspect quality.json and refine the planar overlay regularization')
    used_bodies=set(int(x) for x in np.unique(volume_ids));bodies={k:v for k,v in bodies.items() if k in used_bodies}
    for ref in power['resistors']:
        if not any(b['ref']==ref and b['volume_m3']>0 for b in bodies.values()):raise WorkflowError(f'Missing heater volume: {ref}')
    path=out/'board.msh'
    with path.open('w') as f:
        f.write('$MeshFormat\n2.2 0 8\n$EndMeshFormat\n$PhysicalNames\n'+str(len(bodies)+2)+'\n')
        for k,v in bodies.items():f.write(f'3 {k} "{v["name"]}"\n')
        f.write('2 101 "exposed"\n2 102 "insulated_cut"\n$EndPhysicalNames\n$Nodes\n'+str(len(nodes))+'\n')
        for i,p in enumerate(nodes,1):f.write(f'{i} {p[0]:.12g} {p[1]:.12g} {p[2]:.12g}\n')
        f.write('$EndNodes\n$Elements\n'+str(len(wedges)+len(face_tri)+len(face_quad))+'\n');num=0
        for typ,els,tags in [(6,wedges,volume_ids),(2,face_tri,np.full(len(face_tri),101)),(3,face_quad,quad_ids)]:
            for el,tag in zip(els,tags):
                num+=1;f.write(f'{num} {typ} 2 {tag} {tag} '+' '.join(str(int(v)+1) for v in el)+'\n')
        f.write('$EndElements\n')
    np.savez_compressed(out/'mesh.npz',nodes=nodes,wedges=wedges,body_ids=volume_ids,triangles=face_tri,quads=face_quad,quad_ids=quad_ids)
    meta={'schema':1,'units':'m','construction':'Gmsh planar conformal triangulation swept into linear six-node prisms',
          'nodes':len(nodes),'elements':len(wedges),'bodies':bodies,'materials':materials,'boundaries':{'101':'exposed','102':'insulated_cut'},
          'geometry':case['geometry'],'mechanicals':case.get('mechanicals',[]),
          'through_thickness_mesh':{'z_levels_mm':z,'merged_identical_dielectric_plies':merged_plies},
          'bounds_m':[nodes.min(axis=0).tolist(),nodes.max(axis=0).tolist()]}
    write_json(out/'physical-groups.json',meta)
    # Reopen using Gmsh so format/element support is verified and surface geometry
    # is available to any CAD/mesh viewer without reconstructing the case.
    import gmsh
    gmsh.initialize()
    try:
        gmsh.option.setNumber('General.Terminal',0);gmsh.open(str(path));gmsh.write(str(out/'thermal-surface.stl'))
    finally:gmsh.finalize()
    print(f'Thermal mesh: {len(nodes)} nodes, {len(wedges)} prisms, {len(bodies)} material/source groups',flush=True)
    return meta

if __name__=='__main__':
    import argparse
    p=argparse.ArgumentParser();p.add_argument('board');p.add_argument('case');p.add_argument('power');p.add_argument('output');a=p.parse_args()
    mesh(*[json.loads(Path(x).read_text()) for x in (a.board,a.case,a.power)],a.output)
